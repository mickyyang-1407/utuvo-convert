import Accelerate
import Foundation
import os.lock

/// Host arithmetic precision, independent of CoreAudio transport and third-party AU internals.
public final class MixingPrecision: @unchecked Sendable {
    private let state = OSAllocatedUnfairLock(initialState: false)
    public init() {}
    public var enabled: Bool {
        get { state.withLock { $0 } }
        set { state.withLock { $0 = newValue } }
    }
    @inline(__always) public static func weightedSum(planar: UnsafePointer<UnsafeMutablePointer<Float>>, frame: Int,
                                                     weights: UnsafePointer<Float>, channels: Int, precise: Bool) -> Float {
        if precise {
            var sum: Double = 0
            for c in 0..<channels { sum += Double(weights[c]) * Double(planar[c][frame]) }
            return Float(sum)
        }
        var sum: Float = 0
        for c in 0..<channels { sum += weights[c] * planar[c][frame] }
        return sum
    }
    @inline(__always) public static func gain(_ sample: Float, previous: Float, target: Float, frame: Int, ramp: Int, precise: Bool) -> Float {
        if precise {
            let amount = Double(min(frame, ramp)) / Double(max(1, ramp))
            return Float(Double(sample) * (Double(previous) + (Double(target) - Double(previous)) * amount))
        }
        let amount = Float(min(frame, ramp)) / Float(max(1, ramp))
        return sample * (previous + (target - previous) * amount)
    }

    /// 0163 — the render path's de-click gain over an interleaved buffer, bit-identical to calling
    /// `gain(_:previous:target:frame:ramp:precise:)` for every sample (selfcheck 0163 compares bits).
    /// Once `frame >= ramp` the per-channel gain is constant, so it is computed once and applied
    /// with one multiply per sample (vDSP for Float; exact same IEEE multiply), and skipped when it
    /// is exactly 1. It used to divide and branch per sample: 64 ch × every frame, ~7 % of a core.
    public static func applyGainRamp(interleaved buf: UnsafeMutablePointer<Float>, frames: Int, channels ch: Int,
                                     previous: UnsafePointer<Float>, target: UnsafePointer<Float>,
                                     ramp: Int, precise: Bool) {
        guard frames > 0, ch > 0 else { return }
        guard ramp >= 1 else {
            for i in 0..<frames {
                for c in 0..<ch {
                    buf[i * ch + c] = gain(buf[i * ch + c], previous: previous[c], target: target[c], frame: i, ramp: ramp, precise: precise)
                }
            }
            return
        }
        let rampFrames = min(frames, ramp)
        let steadyFrames = frames - rampFrames
        if precise {
            for i in 0..<rampFrames {
                let amount = Double(i) / Double(ramp)
                for c in 0..<ch {
                    let p = Double(previous[c])
                    buf[i * ch + c] = Float(Double(buf[i * ch + c]) * (p + (Double(target[c]) - p) * amount))
                }
            }
            guard steadyFrames > 0 else { return }
            for c in 0..<ch {
                let p = Double(previous[c])
                let g = p + (Double(target[c]) - p) * (Double(ramp) / Double(ramp))
                if g == 1.0 { continue }
                var idx = rampFrames * ch + c
                for _ in 0..<steadyFrames { buf[idx] = Float(Double(buf[idx]) * g); idx += ch }
            }
        } else {
            for i in 0..<rampFrames {
                let amount = Float(i) / Float(ramp)
                for c in 0..<ch {
                    buf[i * ch + c] = buf[i * ch + c] * (previous[c] + (target[c] - previous[c]) * amount)
                }
            }
            guard steadyFrames > 0 else { return }
            for c in 0..<ch {
                var g = previous[c] + (target[c] - previous[c]) * (Float(ramp) / Float(ramp))
                if g == 1.0 { continue }
                let start = buf + rampFrames * ch + c
                vDSP_vsmul(start, vDSP_Stride(ch), &g, start, vDSP_Stride(ch), vDSP_Length(steadyFrames))
            }
        }
    }
}
