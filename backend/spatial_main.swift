import Foundation
import AudioToolbox
@main struct SpatialOffline {
 static func main() throws {
  let a = CommandLine.arguments
  guard a.count == 5, let rate = Double(a[3]) else { throw NSError(domain:"Spatial",code:1) }
  let layout = a[4]
  let tag: AudioChannelLayoutTag
  let mapping: [Int]
  switch layout {
  case "stereo": tag = kAudioChannelLayoutTag_Stereo; mapping=[0,1]
  case "5.1-side": tag = kAudioChannelLayoutTag_MPEG_5_1_A; mapping=Array(0..<6)
  case "7.1.4-side-first": tag = kAudioChannelLayoutTag_Atmos_7_1_4; mapping=Array(0..<12)
  case "7.1.4-rear-first": tag = kAudioChannelLayoutTag_Atmos_7_1_4; mapping=[0,1,2,3,6,7,4,5,8,9,10,11]
  default: throw NSError(domain:"Unsupported HRTF layout",code:2)
  }
  let raw=try Data(contentsOf:URL(fileURLWithPath:a[1]))
  let samples:[Float]=raw.withUnsafeBytes { b in (0..<(b.count/4)).map { Float(bitPattern:UInt32(littleEndian:b.loadUnaligned(fromByteOffset:$0*4,as:UInt32.self))) } }
  guard samples.count % mapping.count == 0 else { throw NSError(domain:"Partial frame",code:3) }
  let channels=mapping.count, frames=samples.count/channels, block=1024
  let renderer=SpatialRenderer(maxFrames:block)
  guard renderer.configure(sampleRate:rate,inputLayoutTag:tag,bedChannels:mapping) else { throw NSError(domain:"HRTF configuration failed",code:4) }
  let planes=(0..<channels).map {_ in UnsafeMutablePointer<Float>.allocate(capacity:block)}
  defer { planes.forEach{$0.deallocate()} }
  var l=[Float](repeating:0,count:block), r=l, out=[Float]()
  // Explicit 250ms flush; no device/HAL engine is initialized.
  let tail=Int(rate*0.25), total=frames+tail
  for start in stride(from:0,to:total,by:block) {
   let n=min(block,total-start)
   for c in 0..<channels { for f in 0..<n { planes[c][f]=(start+f<frames) ? samples[(start+f)*channels+c] : 0 } }
   let ok=planes.withUnsafeBufferPointer { p in l.withUnsafeMutableBufferPointer { lp in r.withUnsafeMutableBufferPointer { rp in renderer.render(planar:p.baseAddress!,channels:channels,frames:n,outL:lp.baseAddress!,outR:rp.baseAddress!) } } }
   guard ok else { throw NSError(domain:"HRTF render failure",code:5) }
   for f in 0..<n { out.append(l[f]); out.append(r[f]) }
  }
  try out.withUnsafeBytes{Data($0)}.write(to:URL(fileURLWithPath:a[2]),options:.withoutOverwriting)
  print("{\"renderer\":\"Apple AUSpatialMixer HRTF\",\"frames\":\(total),\"tail_frames\":\(tail),\"failures\":\(renderer.renderFailures)}")
 }
}
