// MickyCore/SpatialRenderer.swift — 0030 Apple Spatial renderer (AUSpatialMixer) — full SPHERE iRender parity.
//
// PURPOSE:
//   When the selected main Monitor's renderer == .appleSpatial, every audio
//   buffer pulled from the source goes through this processor BEFORE the
//   standard route → gain → sink path. The Source carries a multi-channel
//   bed (e.g. 7.1.4 = 12 ch) tagged with an Apple AudioChannelLayoutTag
//   (kAudioChannelLayoutTag_Atmos_7_1_4, etc.). AUSpatialMixer renders that
//   bed into binaural stereo using its built-in HRTF.
//
// 0030 — full SPHERE iRender parity (Music/Movie preset × 14 room types × 3 output types
// × 4 HRTF modes × gain). Same architecture as 0019-R1 (AUSpatialMixer + render hot path
// untouched); the additions are NON-HOT-PATH configure-time property writes (each followed by
// a readback compare, see SpatialAU.applyOptions). 3111/3113 writes are FAIL-SOFT (a
// signature without the matching entitlement makes the AU silently ignore them; the renderer
// must still report configure() == true so the rest of the monitor pipeline stays alive).
//
// REAL RENDER IMPLEMENTATION (from tools/spatialspike/spatial.swift + 0019-R1):
//   * C AudioUnit API: AudioComponentFindNext + AudioComponentInstanceNew
//   * Formats: input non-interleaved float32 (nBed channels), output non-interleaved float32 (2 channels)
//   * Input channel layout tag set on kAudioUnitProperty_AudioChannelLayout
//   * Spatial mixer properties (configure-time, set-then-readback):
//       - kAudioUnitProperty_SpatialMixerSourceMode = 3 (AmbienceBed)
//       - kAudioUnitProperty_PresentPreset = factory preset #1 Music / #2 Movie (AUPreset struct)
//       - kAudioUnitProperty_SpatializationAlgorithm = 7 (UseOutputType)
//       - kAudioUnitProperty_SpatialMixerOutputType = options.outputType (1/2/3)
//       - kAudioUnitProperty_ReverbRoomType (3101) + kSpatialMixerParam_GlobalReverbGain (= -120 dB when reverbRoomType == -1)
//       - kAudioUnitProperty_SpatialMixerEnableHeadTracking (3111, fail-soft)
//       - kAudioUnitProperty_SpatialMixerPersonalizedHRTFMode (3113, fail-soft)
//       - kSpatialMixerParam_Gain (gainInternal, dB)
//   * Maximum frames per slice set on global scope
//   * Input render callback installed with Unmanaged<RenderContext> refCon
//   * AudioUnitInitialize
//   * Render hot-path: AudioUnitRender with monotonically advancing ts.mSampleTime (UNTOUCHED)
//   * Zero heap allocation during render.
//   * Threading: configure() builds new instance in pending; render() swaps pending -> active
//     under trylock; retired instances disposed in configure/reset/deinit on non-RT thread.
import Foundation
import AudioToolbox
import CoreAudio
import os.lock

// MARK: - Constants

// Property IDs (AudioUnitProperties.h, macOS 14+).
private let kSpatialMixerSourceMode: AudioUnitPropertyID = 3005
private let kSpatializationAlgorithm: AudioUnitPropertyID = 3000
private let kSpatialMixerOutputType: AudioUnitPropertyID = 3100
private let kSpatialMixerReverbRoomType: AudioUnitPropertyID = 10      // kAudioUnitProperty_ReverbRoomType (AudioUnitProperties.h:3010)
private let kSpatialMixerEnableHeadTracking: AudioUnitPropertyID = 3111
private let kSpatialMixerPersonalizedHRTFMode: AudioUnitPropertyID = 3113
private let kSpatialMixerAnyInputIsUsingPersonalizedHRTF: AudioUnitPropertyID = 3116
private let kAudioUnitProperty_PresentPreset: AudioUnitPropertyID = 36  // AudioUnitProperties.h:917

// Parameter IDs (AudioUnitParameters.h:112/130). Gain is per-INPUT scope (Global returns -10878);
// GlobalReverbGain is Global scope and the AU clamps it to a −40 dB floor (probed 2026-09-04).
private let kSpatialMixerParam_Gain: AudioUnitParameterID = 3
private let kSpatialMixerParam_GlobalReverbGain: AudioUnitParameterID = 9

private let kSourceModeAmbienceBed: UInt32 = 3
private let kSpatializationAlgorithmUseOutputType: UInt32 = 7
private let kSpatialMixerOutputTypeHeadphones: UInt32 = 1
private let kSpatialMixerOutputTypeBuiltInSpeakers: UInt32 = 2
private let kSpatialMixerOutputTypeExternalSpeakers: UInt32 = 3

// AUSpatialMixer factory presets (Music = #1 "Headphone Media Playback Default", Movie = #2
// "Headphone Media Playback Movie"). Verified on the host per ticket 0030 §硬限制.
private let kFactoryPresetMusic: Int32 = 1
private let kFactoryPresetMovie: Int32 = 2

// AUSpatialMixer kReverbRoomType_* constants 0…12. -1 is our own sentinel for "No Reverb"
// (the AU has no such concept — we instead push GlobalReverbGain to -120 dB which audibly
// disables it; see SpatialAU.applyOptions).
private let kReverbRoomTypeNoReverbSentinel: Int = -1
private let kGlobalReverbGainMuteDB: Float = -40   // AU floor (writes below −40 read back as −40)

// MARK: - SpatialOptions

/// SPHERE iRender parity (0030). Codable → RoutingModel.SpatialSettings wraps these with an
/// `hrtfMode` aggregate for the UI; here we keep them orthogonal because the AU property writes
/// are individual.
public struct SpatialOptions: Equatable, Sendable {
    /// Factory preset number: 1 = Music (Headphone Media Playback Default),
    /// 2 = Movie (Headphone Media Playback Movie).
    public enum Preset: UInt8, Sendable, Equatable, Codable {
        case music = 1
        case movie = 2
    }
    public enum OutputType: UInt32, Sendable, Equatable, Codable {
        case headphones = 1
        case builtInSpeakers = 2
        case externalSpeakers = 3
    }
    public enum Personalized: UInt32, Sendable, Equatable, Codable {
        case off = 0
        case on = 1
        case auto = 2
    }

    public var preset: Preset = .music
    /// -1 = No Reverb (sets GlobalReverbGain = -120 dB); 0…12 = kReverbRoomType_*.
    public var reverbRoomType: Int = -1
    public var outputType: OutputType = .headphones
    /// 3111 `EnableHeadTracking` — requires com.apple.developer.coremotion.head-pose;
    /// silently ignored by the AU when the entitlement is missing (per ticket §硬限制).
    public var headTracking: Bool = false
    /// 3113 `PersonalizedHRTFMode` — requires com.apple.developer.spatial-audio.profile-access;
    /// silently ignored by the AU when the entitlement is missing.
    public var personalized: Personalized = .off
    /// gainInternal (dB, -12…+12 — UI clamps). Applied via kSpatialMixerParam_Gain.
    public var gainDB: Float = 0

    public init(preset: Preset = .music,
                reverbRoomType: Int = -1,
                outputType: OutputType = .headphones,
                headTracking: Bool = false,
                personalized: Personalized = .off,
                gainDB: Float = 0) {
        self.preset = preset
        self.reverbRoomType = reverbRoomType
        self.outputType = outputType
        self.headTracking = headTracking
        self.personalized = personalized
        self.gainDB = gainDB
    }
}

/// Readback snapshot of the AUSpatialMixer state. Returned by `spatialStatus()`.
public struct SpatialStatus: Equatable, Sendable {
    public var headTrackingActive: Bool
    public var personalizedActive: Bool
    public var presetApplied: Bool
}

// MARK: - Render Context for AURenderCallback

private final class SpatialRenderContext {
    var abl: UnsafeMutableAudioBufferListPointer

    init(_ abl: UnsafeMutableAudioBufferListPointer) {
        self.abl = abl
    }
}

private let spatialInputProc: AURenderCallback = { refCon, _, _, _, nFrames, ioData -> OSStatus in
    guard let ioData = ioData else { return -1 }
    let ctx = Unmanaged<SpatialRenderContext>.fromOpaque(refCon).takeUnretainedValue()
    let dst = UnsafeMutableAudioBufferListPointer(ioData)
    let n = min(dst.count, ctx.abl.count)
    for k in 0..<n {
        dst[k].mData = ctx.abl[k].mData
        dst[k].mDataByteSize = nFrames * 4
    }
    return noErr
}

// MARK: - SpatialAU

private final class SpatialAU {
    let au: AudioUnit
    let bedChannels: [Int]
    let sampleRate: Double
    let inputLayoutTag: AudioChannelLayoutTag
    let maxFrames: Int
    let options: SpatialOptions

    let inStore: [UnsafeMutablePointer<Float>]
    let inABL: UnsafeMutableAudioBufferListPointer
    let outStore: [UnsafeMutablePointer<Float>]
    let outABL: UnsafeMutableAudioBufferListPointer
    let context: SpatialRenderContext
    var sampleTime: Double = 0.0

    // Failure tracking — fail-soft (3111/3113) writes do NOT mark the AU as broken; the
    // configure call only returns false when a hard property write fails.
    private(set) var headTrackingRequested: Bool = false
    private(set) var headTrackingActive: Bool = false
    private(set) var personalizedRequested: SpatialOptions.Personalized = .off
    private(set) var personalizedActive: Bool = false
    private(set) var presetApplied: Bool = false

    init?(sampleRate: Double,
          inputLayoutTag: AudioChannelLayoutTag,
          bedChannels: [Int],
          maxFrames: Int,
          options: SpatialOptions) {
        let nBed = UInt32(bedChannels.count)
        guard nBed > 0 && nBed <= 16 else { return nil }

        self.sampleRate = sampleRate
        self.inputLayoutTag = inputLayoutTag
        self.bedChannels = bedChannels
        self.maxFrames = maxFrames
        self.options = options

        // 1. AudioComponentFindNext & AudioComponentInstanceNew
        var desc = AudioComponentDescription(
            componentType: kAudioUnitType_Mixer,
            componentSubType: kAudioUnitSubType_SpatialMixer,
            componentManufacturer: kAudioUnitManufacturer_Apple,
            componentFlags: 0,
            componentFlagsMask: 0
        )
        guard let comp = AudioComponentFindNext(nil, &desc) else { return nil }
        var unitOpt: AudioUnit? = nil
        let newStatus = AudioComponentInstanceNew(comp, &unitOpt)
        guard newStatus == noErr, let au = unitOpt else { return nil }
        self.au = au

        // 2. Formats: non-interleaved Float32
        func makeASBD(_ ch: UInt32) -> AudioStreamBasicDescription {
            AudioStreamBasicDescription(
                mSampleRate: sampleRate,
                mFormatID: kAudioFormatLinearPCM,
                mFormatFlags: kAudioFormatFlagIsFloat | kAudioFormatFlagIsPacked | kAudioFormatFlagIsNonInterleaved,
                mBytesPerPacket: 4,
                mFramesPerPacket: 1,
                mBytesPerFrame: 4,
                mChannelsPerFrame: ch,
                mBitsPerChannel: 32,
                mReserved: 0
            )
        }
        var inFmt = makeASBD(nBed)
        var outFmt = makeASBD(2)
        var status = AudioUnitSetProperty(
            au,
            kAudioUnitProperty_StreamFormat,
            kAudioUnitScope_Input,
            0,
            &inFmt,
            UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
        )
        guard status == noErr else {
            AudioComponentInstanceDispose(au)
            return nil
        }

        status = AudioUnitSetProperty(
            au,
            kAudioUnitProperty_StreamFormat,
            kAudioUnitScope_Output,
            0,
            &outFmt,
            UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
        )
        guard status == noErr else {
            AudioComponentInstanceDispose(au)
            return nil
        }

        // 3. AudioChannelLayout (Input scope 0)
        var layout = AudioChannelLayout()
        layout.mChannelLayoutTag = inputLayoutTag
        status = AudioUnitSetProperty(
            au,
            kAudioUnitProperty_AudioChannelLayout,
            kAudioUnitScope_Input,
            0,
            &layout,
            UInt32(MemoryLayout<AudioChannelLayout>.size)
        )
        guard status == noErr else {
            AudioComponentInstanceDispose(au)
            return nil
        }

        // 4. Properties: SourceMode, SpatializationAlgorithm, OutputType, MaximumFramesPerSlice.
        func setU32(_ pid: AudioUnitPropertyID, _ v: UInt32, _ scope: AudioUnitScope, _ el: AudioUnitElement) -> OSStatus {
            var vv = v
            return AudioUnitSetProperty(au, pid, scope, el, &vv, 4)
        }
        guard setU32(kSpatialMixerSourceMode, kSourceModeAmbienceBed, kAudioUnitScope_Input, 0) == noErr else {
            AudioComponentInstanceDispose(au)
            return nil
        }
        guard setU32(kSpatializationAlgorithm, kSpatializationAlgorithmUseOutputType, kAudioUnitScope_Input, 0) == noErr else {
            AudioComponentInstanceDispose(au)
            return nil
        }
        let outTypeRaw: UInt32 = {
            switch options.outputType {
            case .headphones: return kSpatialMixerOutputTypeHeadphones
            case .builtInSpeakers: return kSpatialMixerOutputTypeBuiltInSpeakers
            case .externalSpeakers: return kSpatialMixerOutputTypeExternalSpeakers
            }
        }()
        guard setU32(kSpatialMixerOutputType, outTypeRaw, kAudioUnitScope_Global, 0) == noErr else {
            AudioComponentInstanceDispose(au)
            return nil
        }

        var maxF = UInt32(maxFrames)
        guard AudioUnitSetProperty(au, kAudioUnitProperty_MaximumFramesPerSlice, kAudioUnitScope_Global, 0, &maxF, 4) == noErr else {
            AudioComponentInstanceDispose(au)
            return nil
        }

        // 5. Preallocate input buffers & ABL
        let inList = AudioBufferList.allocate(maximumBuffers: Int(nBed))
        var inBuffers: [UnsafeMutablePointer<Float>] = []
        inBuffers.reserveCapacity(Int(nBed))
        for i in 0..<Int(nBed) {
            let p = UnsafeMutablePointer<Float>.allocate(capacity: maxFrames)
            p.initialize(repeating: 0, count: maxFrames)
            inBuffers.append(p)
            inList[i] = AudioBuffer(mNumberChannels: 1, mDataByteSize: UInt32(maxFrames * 4), mData: UnsafeMutableRawPointer(p))
        }
        self.inStore = inBuffers
        self.inABL = inList

        let ctx = SpatialRenderContext(inList)
        self.context = ctx

        // 6. Set render callback
        var cb = AURenderCallbackStruct(
            inputProc: spatialInputProc,
            inputProcRefCon: Unmanaged.passUnretained(ctx).toOpaque()
        )
        guard AudioUnitSetProperty(
            au,
            kAudioUnitProperty_SetRenderCallback,
            kAudioUnitScope_Input,
            0,
            &cb,
            UInt32(MemoryLayout<AURenderCallbackStruct>.size)
        ) == noErr else {
            AudioComponentInstanceDispose(au)
            return nil
        }

        // 7. Preallocate output buffers & ABL
        let outList = AudioBufferList.allocate(maximumBuffers: 2)
        var outBuffers: [UnsafeMutablePointer<Float>] = []
        outBuffers.reserveCapacity(2)
        for i in 0..<2 {
            let p = UnsafeMutablePointer<Float>.allocate(capacity: maxFrames)
            p.initialize(repeating: 0, count: maxFrames)
            outBuffers.append(p)
            outList[i] = AudioBuffer(mNumberChannels: 1, mDataByteSize: UInt32(maxFrames * 4), mData: UnsafeMutableRawPointer(p))
        }
        self.outStore = outBuffers
        self.outABL = outList

        // 8. Options (preset / reverb / HT / personalized / gain) — before Initialize (ticket §A);
        //    every set is read back (see applyOptions).
        applyOptions()

        // 9. Initialize AudioUnit

        guard AudioUnitInitialize(au) == noErr else {
            AudioComponentInstanceDispose(au)
            return nil
        }
    }

    /// Configure-time property writes for SPHERE iRender parity. Each non-fail-soft write
    /// performs a readback compare; mismatch is logged but does NOT abort the AU (the readback
    /// failure cases on Apple Silicon in 2026-09 host were the PresentPreset round-trip
    /// occasionally returning the factory's name string and confusing strcmp, hence we use the
    /// numeric preset number for the compare).
    /// Applied before AudioUnitInitialize (ticket §A order). Every set is read back; a mismatch is
    /// recorded in `optionsFailures` and the corresponding status flag stays false.
    private(set) var optionsFailures: Int = 0
    private(set) var reverbApplied: Bool = false
    private(set) var gainApplied: Bool = false

    private func applyOptions() {
        // 9a. PresentPreset (factory #1 Music / #2 Movie). Probed 2026-09-04: the set is accepted
        // before and after AudioUnitInitialize and does NOT clobber SourceMode/OutputType/Reverb.
        let presetNumber: Int32 = (options.preset == .movie) ? kFactoryPresetMovie : kFactoryPresetMusic
        var preset = AUPreset(presetNumber: presetNumber, presetName: nil)
        let setPS = AudioUnitSetProperty(au, kAudioUnitProperty_PresentPreset, kAudioUnitScope_Global, 0, &preset, UInt32(MemoryLayout<AUPreset>.size))
        var readPreset = AUPreset(presetNumber: -1, presetName: nil)
        var readPresetSize = UInt32(MemoryLayout<AUPreset>.size)
        let getPS = AudioUnitGetProperty(au, kAudioUnitProperty_PresentPreset, kAudioUnitScope_Global, 0, &readPreset, &readPresetSize)
        presetApplied = (setPS == noErr && getPS == noErr && readPreset.presetNumber == presetNumber)
        if !presetApplied { optionsFailures += 1; NSLog("[SpatialRenderer] PresentPreset \(presetNumber): set=\(setPS) get=\(getPS) read=\(readPreset.presetNumber)") }

        // 9b. ReverbRoomType (property 10) + GlobalReverbGain (param 9, Global). -1 = No Reverb →
        // GlobalReverbGain to the AU floor (−40 dB); otherwise room type + 0 dB reverb gain.
        let reverb = options.reverbRoomType
        var reverbOK = true
        if reverb != kReverbRoomTypeNoReverbSentinel {
            var rv = UInt32(max(0, min(12, reverb)))
            let st = AudioUnitSetProperty(au, kSpatialMixerReverbRoomType, kAudioUnitScope_Global, 0, &rv, 4)
            var back: UInt32 = 999; var bs: UInt32 = 4
            let gt = AudioUnitGetProperty(au, kSpatialMixerReverbRoomType, kAudioUnitScope_Global, 0, &back, &bs)
            reverbOK = (st == noErr && gt == noErr && back == rv)
            let sg = AudioUnitSetParameter(au, kSpatialMixerParam_GlobalReverbGain, kAudioUnitScope_Global, 0, 0.0, 0)
            reverbOK = reverbOK && (sg == noErr)
        } else {
            let sg = AudioUnitSetParameter(au, kSpatialMixerParam_GlobalReverbGain, kAudioUnitScope_Global, 0, kGlobalReverbGainMuteDB, 0)
            var back: AudioUnitParameterValue = 0
            let gg = AudioUnitGetParameter(au, kSpatialMixerParam_GlobalReverbGain, kAudioUnitScope_Global, 0, &back)
            reverbOK = (sg == noErr && gg == noErr && back <= kGlobalReverbGainMuteDB + 0.5)
        }
        reverbApplied = reverbOK
        if !reverbOK { optionsFailures += 1; NSLog("[SpatialRenderer] reverb apply failed (room \(reverb))") }

        // 9c. HeadTracking (3111, fail-soft): the AU accepts the write even on an unsigned build; the
        // OS only feeds motion when the head-pose entitlement is present. `headTrackingActive` is the
        // raw AU readback; EngineBridge AND's it with the entitlement gate for the UI.
        var ht: UInt32 = options.headTracking ? 1 : 0
        let setHT = AudioUnitSetProperty(au, kSpatialMixerEnableHeadTracking, kAudioUnitScope_Global, 0, &ht, 4)
        headTrackingRequested = options.headTracking
        if setHT == noErr {
            var readHT: UInt32 = 0; var readSize: UInt32 = 4
            let getHT = AudioUnitGetProperty(au, kSpatialMixerEnableHeadTracking, kAudioUnitScope_Global, 0, &readHT, &readSize)
            headTrackingActive = (getHT == noErr && readHT != 0)
        } else {
            headTrackingActive = false
        }

        // 9d. Personalized HRTF (3113, fail-soft) + 3116 cross-check.
        var pm = options.personalized.rawValue
        let setPM = AudioUnitSetProperty(au, kSpatialMixerPersonalizedHRTFMode, kAudioUnitScope_Global, 0, &pm, 4)
        personalizedRequested = options.personalized
        if setPM == noErr {
            var readPM: UInt32 = 0; var readSize: UInt32 = 4
            let getPM = AudioUnitGetProperty(au, kSpatialMixerPersonalizedHRTFMode, kAudioUnitScope_Global, 0, &readPM, &readSize)
            if getPM == noErr && readPM != 0 {
                personalizedActive = true
            } else {
                var anyP: UInt32 = 0; var anySize: UInt32 = 4
                let getAny = AudioUnitGetProperty(au, kSpatialMixerAnyInputIsUsingPersonalizedHRTF, kAudioUnitScope_Global, 0, &anyP, &anySize)
                personalizedActive = (getAny == noErr && anyP != 0)
            }
        } else {
            personalizedActive = false
        }

        // 9e. Gain (param 3, INPUT scope, dB) with readback.
        let clampedGain = max(-12.0, min(12.0, options.gainDB))
        let sgain = AudioUnitSetParameter(au, kSpatialMixerParam_Gain, kAudioUnitScope_Input, 0, clampedGain, 0)
        var backGain: AudioUnitParameterValue = 999
        let ggain = AudioUnitGetParameter(au, kSpatialMixerParam_Gain, kAudioUnitScope_Input, 0, &backGain)
        gainApplied = (sgain == noErr && ggain == noErr && abs(backGain - clampedGain) < 0.01)
        if !gainApplied { optionsFailures += 1; NSLog("[SpatialRenderer] gain apply failed set=\(sgain) get=\(ggain) read=\(backGain)") }
    }

    func dispose() {
        _ = AudioUnitUninitialize(au)
        _ = AudioComponentInstanceDispose(au)
        for p in inStore {
            p.deinitialize(count: maxFrames)
            p.deallocate()
        }
        free(inABL.unsafeMutablePointer)
        for p in outStore {
            p.deinitialize(count: maxFrames)
            p.deallocate()
        }
        free(outABL.unsafeMutablePointer)
    }

    deinit {
        // Fallback dispose if not already retired
        // Note: dispose() is normally explicitly called from non-RT threads
    }
}

// MARK: - SpatialRenderer

public final class SpatialRenderer {
    public init(maxFrames: Int = 4096) {
        let cap = max(64, min(maxFrames, 16384))
        self.maxFrames = cap
    }

    deinit {
        // deinit runs on caller thread (non-RT)
        pendingAU?.dispose()
        activeAU?.dispose()
        for au in retiredAUs {
            au.dispose()
        }
    }

    public private(set) var isConfigured: Bool = false
    public private(set) var renderFailures: UInt64 = 0
    /// 0184 — blocks where the AU raised `kAudioUnitRenderAction_OutputIsSilence` and `render()`
    /// wrote zeros instead of copying the (by contract undefined) buffer. Diagnostic only, and
    /// the only way a test can tell "we read the flag" from "the AU happened to clear the buffer
    /// anyway" — without it the gate would be green either way. Same non-atomic accumulation as
    /// `renderFailures`: audio thread writes, control thread reads for reporting.
    public private(set) var silenceBlocks: UInt64 = 0
    /// 0184 — blocks where the AU handed back its own output storage instead of ours, so the
    /// copy had to come from `mData` rather than `outStore`. Diagnostic only.
    public private(set) var outputPointerReplacements: UInt64 = 0
    /// 0035 — bumped on every successful configure() (= a brand-new AUSpatialMixer instance). Lets the
    /// app layer prove that a slot-only change (Flip L/R) did NOT rebuild the AU.
    public private(set) var configureCount: UInt64 = 0
    public let maxFrames: Int

    private var unfairLock = os_unfair_lock_s()
    private var pendingAU: SpatialAU?
    private var activeAU: SpatialAU?
    private var retiredAUs: [SpatialAU] = { var a = [SpatialAU](); a.reserveCapacity(8); return a }()

    private var configuredLayoutTag: AudioChannelLayoutTag = kAudioChannelLayoutTag_Stereo
    private var configuredBedChannels: Int = 0
    private var configuredSampleRate: Double = 0
    private var lastSpatialOptions: SpatialOptions = SpatialOptions()

    // MARK: - configure

    /// 0030 — new signature with full SPHERE iRender options. The old 3-arg call is preserved
    /// as a wrapper that uses the default `SpatialOptions()`.
    public func configure(sampleRate: Double,
                          inputLayoutTag: AudioChannelLayoutTag,
                          bedChannels: [Int],
                          options: SpatialOptions) -> Bool {
        let sr = sampleRate > 0 ? sampleRate : 48000.0
        let nBed = bedChannels.count
        guard nBed > 0 && nBed <= 16 else {
            return false
        }

        // Clean up any previously retired instances on this non-RT thread. keepingCapacity: the audio
        // thread appends to `retiredAUs` on swap and must never trigger a heap allocation.
        os_unfair_lock_lock(&unfairLock)
        let oldRetired = retiredAUs
        retiredAUs.removeAll(keepingCapacity: true)
        os_unfair_lock_unlock(&unfairLock)
        for au in oldRetired {
            au.dispose()
        }

        // Build new instance outside lock
        guard let newSpAU = SpatialAU(
            sampleRate: sr,
            inputLayoutTag: inputLayoutTag,
            bedChannels: bedChannels,
            maxFrames: maxFrames,
            options: options
        ) else {
            return false
        }

        os_unfair_lock_lock(&unfairLock)
        if let prev = pendingAU {
            retiredAUs.append(prev)
        }
        pendingAU = newSpAU
        configuredLayoutTag = inputLayoutTag
        configuredBedChannels = nBed
        configuredSampleRate = sr
        lastSpatialOptions = options
        isConfigured = true
        configureCount &+= 1
        os_unfair_lock_unlock(&unfairLock)

        return true
    }

    /// 0019-R1 / 0030 — legacy 3-arg signature preserved. Default SpatialOptions match the
    /// 0019-R1 behaviour (Music preset, headphones, no reverb, no head tracking, no personalized).
    public func configure(sampleRate: Double,
                          inputLayoutTag: AudioChannelLayoutTag,
                          bedChannels: [Int]) -> Bool {
        return configure(sampleRate: sampleRate,
                         inputLayoutTag: inputLayoutTag,
                         bedChannels: bedChannels,
                         options: SpatialOptions())
    }

    // MARK: - render (HOT PATH — UNTOUCHED per ticket)

    public func render(planar: UnsafePointer<UnsafeMutablePointer<Float>>,
                       channels: Int,
                       frames: Int,
                       outL: UnsafeMutablePointer<Float>,
                       outR: UnsafeMutablePointer<Float>) -> Bool {
        guard frames > 0 else {
            return true
        }

        // Trylock exchange: promote pendingAU -> activeAU, retire old activeAU
        if os_unfair_lock_trylock(&unfairLock) {
            if let pending = pendingAU {
                if let prior = activeAU {
                    retiredAUs.append(prior)
                }
                activeAU = pending
                pendingAU = nil
            }
            os_unfair_lock_unlock(&unfairLock)
        }

        guard let active = activeAU else {
            renderFailures &+= 1
            outL.update(repeating: 0, count: frames)
            outR.update(repeating: 0, count: frames)
            return false
        }

        // Chunking if frames > maxFrames
        let chunkSize = active.maxFrames
        var framesRemaining = frames
        var frameOffset = 0

        let nBed = active.bedChannels.count
        let inStore = active.inStore
        let outStore = active.outStore
        let outABLPtr = active.outABL.unsafeMutablePointer

        while framesRemaining > 0 {
            let currentSlice = min(framesRemaining, chunkSize)
            let currentBytes = UInt32(currentSlice * 4)

            // Copy bed channels into active.inStore
            for j in 0..<nBed {
                let srcCh = active.bedChannels[j]
                let dstPtr = inStore[j]
                if srcCh < 0 || srcCh >= channels {
                    memset(dstPtr, 0, Int(currentBytes))
                } else {
                    let srcPtr = planar[srcCh].advanced(by: frameOffset)
                    memcpy(dstPtr, srcPtr, Int(currentBytes))
                }
                active.inABL[j].mDataByteSize = currentBytes
                active.inABL[j].mData = UnsafeMutableRawPointer(dstPtr)
            }

            var ts = AudioTimeStamp()
            ts.mSampleTime = active.sampleTime
            ts.mFlags = .sampleTimeValid
            active.sampleTime += Double(currentSlice)

            // 0184 — restore host-owned storage AND pre-zero it before every call. An AU that
            // returns without writing (which kAudioUnitRenderAction_OutputIsSilence explicitly
            // permits) must read back as silence, never as the previous block. Same pattern as
            // AudioUnitRack.process(). memset only — no allocation, RT-safe.
            var flags = AudioUnitRenderActionFlags()
            for k in 0..<2 {
                outStore[k].update(repeating: 0, count: currentSlice)
                active.outABL[k].mDataByteSize = currentBytes
                active.outABL[k].mData = UnsafeMutableRawPointer(outStore[k])
            }

            let status = AudioUnitRender(active.au, &flags, &ts, 0, UInt32(currentSlice), outABLPtr)
            if status != noErr {
                renderFailures &+= 1
                outL.advanced(by: frameOffset).update(repeating: 0, count: framesRemaining)
                outR.advanced(by: frameOffset).update(repeating: 0, count: framesRemaining)
                return false
            }

            // 0184 — the AU declared its output silent, so the buffer contents are undefined by
            // contract. Write zeros rather than copying whatever happens to be there.
            // AUSpatialMixer raises this from the second all-silent block onwards.
            if (flags.rawValue & AudioUnitRenderActionFlags.unitRenderAction_OutputIsSilence.rawValue) != 0 {
                silenceBlocks &+= 1
                outL.advanced(by: frameOffset).update(repeating: 0, count: currentSlice)
                outR.advanced(by: frameOffset).update(repeating: 0, count: currentSlice)
                frameOffset += currentSlice
                framesRemaining -= currentSlice
                continue
            }

            // 0184 — copy from what the AU actually filled. It may hand back its own storage
            // instead of ours (AUSpatialMixer provably does when handed a null mData), in which
            // case outStore is stale. mDataByteSize is measured to come back equal to what we
            // asked for; a short buffer is a broken AU, so fail closed like a render error.
            guard let srcL = active.outABL[0].mData,
                  let srcR = active.outABL[1].mData,
                  active.outABL[0].mDataByteSize >= currentBytes,
                  active.outABL[1].mDataByteSize >= currentBytes else {
                renderFailures &+= 1
                outL.advanced(by: frameOffset).update(repeating: 0, count: framesRemaining)
                outR.advanced(by: frameOffset).update(repeating: 0, count: framesRemaining)
                return false
            }
            if srcL != UnsafeMutableRawPointer(outStore[0]) || srcR != UnsafeMutableRawPointer(outStore[1]) {
                outputPointerReplacements &+= 1
            }
            memcpy(outL.advanced(by: frameOffset), srcL, Int(currentBytes))
            memcpy(outR.advanced(by: frameOffset), srcR, Int(currentBytes))

            frameOffset += currentSlice
            framesRemaining -= currentSlice
        }

        return true
    }

    // MARK: - reset

    /// Non-RT. Retires the active/pending instances but does NOT dispose the most recent ones: `render()`
    /// reads `activeAU` outside the lock, so an instance that was active a moment ago may still be inside
    /// AudioUnitRender on the audio thread. Callers must publish "spatial off" to the tap BEFORE calling
    /// reset(); disposal happens at the next configure() / deinit, or here for entries retired earlier.
    public func reset() {
        os_unfair_lock_lock(&unfairLock)
        // Anything retired before this call is guaranteed out of use (a later swap already happened).
        let olderRetired = retiredAUs
        retiredAUs.removeAll(keepingCapacity: true)
        if let prev = pendingAU {
            retiredAUs.append(prev)
            pendingAU = nil
        }
        if let prev = activeAU {
            retiredAUs.append(prev)
            activeAU = nil
        }
        isConfigured = false
        os_unfair_lock_unlock(&unfairLock)

        for au in olderRetired {
            au.dispose()
        }
    }

    /// 0104 — highest SOURCE channel index this renderer will read, or −1 when
    /// nothing is configured. Read-only, control thread, existing lock; no DSP,
    /// render or AU behaviour is touched and `snapshot()` is unchanged.
    ///
    /// Why the count in `snapshot().bedChannels` is not enough: the bed is a
    /// MAP, not a prefix (`render` reads `active.bedChannels[j]`). A stereo bed
    /// pinned to source channels 14 and 15 has count 2 but needs channel 15, so
    /// the capture-side width gate would have accepted a 2-channel callback and
    /// silently fed the renderer nothing. Both the active and the pending
    /// This describes the INTENDED effective configuration: `pendingAU` wins
    /// when present, because that is the one `render` promotes on its next
    /// swap. Taking the max of active and pending instead would let a retired
    /// high-channel setup veto a valid new compact one forever — with the
    /// engine stopped nothing ever calls `render`, so the promotion that would
    /// clear the old value never happens (0104 start-order review).
    public func highestBedSourceChannel() -> Int {
        os_unfair_lock_lock(&unfairLock)
        var hi = -1
        if let au = pendingAU ?? activeAU { for ch in au.bedChannels where ch > hi { hi = ch } }
        os_unfair_lock_unlock(&unfairLock)
        return hi
    }

    public func snapshot() -> (configured: Bool, layoutTag: UInt32, bedChannels: Int, sampleRate: Double, failures: UInt64) {
        os_unfair_lock_lock(&unfairLock)
        let res = (isConfigured, configuredLayoutTag, configuredBedChannels, configuredSampleRate, renderFailures)
        os_unfair_lock_unlock(&unfairLock)
        return res
    }

    // MARK: - 0030 status / readback

    /// Live AU readback of the three capability-gated knobs. Returned tuple is safe to log and
    /// to feed into the UI status badges — `headTrackingActive` is the readback of 3111, not
    /// the requested `options.headTracking`; if the entitlement is missing the AU silently ignores
    /// the set and the readback returns 0 → `headTrackingActive == false`. Same for personalized
    /// (3113 + 3116 cross-check).
    /// 0030 — did reverb/gain sets read back correctly (selfcheck 148).
    public func optionsReadback() -> (reverbApplied: Bool, gainApplied: Bool, failures: Int)? {
        os_unfair_lock_lock(&unfairLock)
        let au = activeAU ?? pendingAU
        os_unfair_lock_unlock(&unfairLock)
        guard let au = au else { return nil }
        return (au.reverbApplied, au.gainApplied, au.optionsFailures)
    }

    public func spatialStatus() -> SpatialStatus {
        os_unfair_lock_lock(&unfairLock)
        let au = activeAU ?? pendingAU
        os_unfair_lock_unlock(&unfairLock)
        guard let au = au else {
            return SpatialStatus(headTrackingActive: false, personalizedActive: false, presetApplied: false)
        }
        return SpatialStatus(
            headTrackingActive: au.headTrackingActive,
            personalizedActive: au.personalizedActive,
            presetApplied: au.presetApplied
        )
    }

    /// Direct AU property read for selfcheck diagnostics. Returns the live values of the four
    /// AU state properties that the renderer exposes (PresentPreset's presetNumber, the two
    /// reverb knobs, and 3111/3113). Pulls from the active AU under the lock; returns nil when
    /// no AU is currently configured.
    public func debugReadback() -> (presetNumber: Int32,
                                    reverbRoomType: UInt32,
                                    globalReverbGainDB: Float,
                                    headTracking: Bool,
                                    personalizedHRTFMode: UInt32,
                                    anyInputIsUsingPersonalizedHRTF: Bool,
                                    gainDB: Float)? {
        os_unfair_lock_lock(&unfairLock)
        let au = activeAU ?? pendingAU
        os_unfair_lock_unlock(&unfairLock)
        guard let au = au else { return nil }

        // PresentPreset
        var preset = AUPreset(presetNumber: 0, presetName: nil)
        var presetSize = UInt32(MemoryLayout<AUPreset>.size)
        _ = AudioUnitGetProperty(au.au, kAudioUnitProperty_PresentPreset, kAudioUnitScope_Global, 0, &preset, &presetSize)

        // ReverbRoomType
        var rv: UInt32 = 0
        var rvSize: UInt32 = 4
        _ = AudioUnitGetProperty(au.au, kSpatialMixerReverbRoomType, kAudioUnitScope_Global, 0, &rv, &rvSize)

        // GlobalReverbGain (param). AudioUnitGetParameter takes only an outValue pointer (Float).
        var globalReverb: AudioUnitParameterValue = 0
        _ = AudioUnitGetParameter(au.au, kSpatialMixerParam_GlobalReverbGain, kAudioUnitScope_Global, 0, &globalReverb)

        // HeadTracking
        var ht: UInt32 = 0
        var htSize: UInt32 = 4
        _ = AudioUnitGetProperty(au.au, kSpatialMixerEnableHeadTracking, kAudioUnitScope_Global, 0, &ht, &htSize)

        // Personalized HRTF mode
        var pm: UInt32 = 0
        var pmSize: UInt32 = 4
        _ = AudioUnitGetProperty(au.au, kSpatialMixerPersonalizedHRTFMode, kAudioUnitScope_Global, 0, &pm, &pmSize)

        // AnyInputIsUsingPersonalizedHRTF (3116)
        var anyP: UInt32 = 0
        var anyPSize: UInt32 = 4
        _ = AudioUnitGetProperty(au.au, kSpatialMixerAnyInputIsUsingPersonalizedHRTF, kAudioUnitScope_Global, 0, &anyP, &anyPSize)

        // Gain param (read raw; AU stores Float32 in dB).
        var gain: AudioUnitParameterValue = 0
        _ = AudioUnitGetParameter(au.au, kSpatialMixerParam_Gain, kAudioUnitScope_Input, 0, &gain)

        return (
            presetNumber: preset.presetNumber,
            reverbRoomType: rv,
            globalReverbGainDB: globalReverb,
            headTracking: ht != 0,
            personalizedHRTFMode: pm,
            anyInputIsUsingPersonalizedHRTF: anyP != 0,
            gainDB: gain
        )
    }

    /// Snapshot of the SpatialOptions the renderer was last configured with. Used by the app layer
    /// to keep UI state in sync after a fresh configure() round-trip (e.g. after a routing rebuild).
    public func configuredOptions() -> SpatialOptions {
        os_unfair_lock_lock(&unfairLock)
        let opts = lastSpatialOptions
        os_unfair_lock_unlock(&unfairLock)
        return opts
    }
}
