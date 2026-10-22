import AppKit
import SwiftUI
import ImageIO
import QuartzCore

/// Quiet interface caption ink; glass captions use their separate filled inlay.
struct EtchedLightInk:ViewModifier {
 func body(content:Content)->some View{
  content.foregroundStyle(Color(red:0.22,green:0.27,blue:0.28))
 }
}
extension View{func etchedLightInk()->some View{modifier(EtchedLightInk())}}

/// Native Traditional Chinese outlines, with restrained weights and no rounded substitution.
enum ConverterTypography {
 static func font(_ size:CGFloat,weight:Font.Weight = .regular)->Font {
  let name=weight == .semibold || weight == .bold ? "PingFangTC-Semibold":weight == .medium ? "PingFangTC-Medium":"PingFangTC-Regular"
  return NSFont(name:name,size:size).map{Font($0)} ?? .system(size:size,weight:weight)
 }
}

/// Filled caption ink, light on the deeper red glass and dark elsewhere.
/// Native text remains live and accessible; this is composited lettering, not
/// text baked into the Blender mesh or live desktop refraction.
struct GlassInlaidInk:ViewModifier {
 let light:Bool
 func body(content:Content)->some View{
  content.foregroundStyle(light ? Color.white:Color(red:0.02,green:0.03,blue:0.04))
 }
}
extension View{func glassInlaidInk(light:Bool = false)->some View{modifier(GlassInlaidInk(light:light))}}

/// All artwork is decoded once. Playback never opens a file or rebuilds a mesh.
@MainActor final class IrisArtwork {
 static let shared = IrisArtwork()
 let frames:[CGImage]
 let center:CGImage?
 let pressedCenter:CGImage?
 let backdrop:CGImage?
 let fiveFrames:[CGImage]
 let softFrames:[CGImage]
 let softFiveFrames:[CGImage]
 let softCenter:CGImage?
 let softPressedCenter:CGImage?
 var available:Bool{frames.count == 73 && fiveFrames.count == 73 && center != nil && pressedCenter != nil && backdrop != nil}
 var adjustable:Bool{available && softFrames.count == 73 && softFiveFrames.count == 73 && softCenter != nil && softPressedCenter != nil}
 private init(){
  let root=Bundle.main.resourceURL?.appendingPathComponent("IrisAssets")
  // Preserve Blender's authored optical colour and AgX output exactly.
  func decode(_ name:String)->CGImage?{
   guard let url=root?.appendingPathComponent(name),let source=CGImageSourceCreateWithURL(url as CFURL,nil) else{return nil}
   return CGImageSourceCreateImageAtIndex(source,0,[kCGImageSourceShouldCacheImmediately:true] as CFDictionary)
  }
  frames=(0..<73).compactMap{decode(String(format:"frames/iris-%03d.png",$0))}
  center=decode("center-normal.png");pressedCenter=decode("center-pressed.png")
  backdrop=decode("backdrop.png")
  fiveFrames=(0..<73).compactMap{decode(String(format:"layout-5/frames/iris-%03d.png",$0))}
  softFrames=(0..<73).compactMap{decode(String(format:"matte/frames/iris-%03d.png",$0))}
  softFiveFrames=(0..<73).compactMap{decode(String(format:"matte/layout-5/frames/iris-%03d.png",$0))}
  softCenter=decode("matte/center-normal.png");softPressedCenter=decode("matte/center-pressed.png")
 }
}

/// A reversible playhead through the physical Blender hinge poses.
/// Retargeting retains the current pose and velocity; reduced motion jumps to rest.
@MainActor final class IrisMotion:ObservableObject {
 @Published private(set) var progress=0.0
 private var target=0.0
 private var velocity=0.0
 private var lastTick=0.0
 private var timer:Timer?
 func setOpen(_ open:Bool,reduced:Bool){
  target=open ? 1:0
  if reduced{timer?.invalidate();timer=nil;velocity=0;progress=target;return}
  guard abs(target-progress)>0.00001,timer == nil else{return}
  lastTick=CACurrentMediaTime()
  let next=Timer(timeInterval:1/60,repeats:true){[weak self] _ in
   MainActor.assumeIsolated{self?.tick()}
  }
  timer=next;RunLoop.main.add(next,forMode:.common)
 }
 private func tick(){
  let now=CACurrentMediaTime(),dt=min(1/20,now-lastTick);lastTick=now
  let desired=target == 1 ? 1/1.5:-1/0.95
  velocity += (desired-velocity)*min(1,dt*20)
  let next=progress+velocity*dt
  if (target == 1 && next>=1) || (target == 0 && next<=0){
   progress=target;velocity=0;timer?.invalidate();timer=nil
  }else{progress=min(1,max(0,next))}
 }
 deinit{timer?.invalidate()}
}

@MainActor final class IrisRenderView:NSView {
 private let softLeafLayer=CALayer(),leafLayer=CALayer(),softCenterLayer=CALayer(),centerLayer=CALayer()
 override init(frame:NSRect){
  super.init(frame:frame);wantsLayer=true
  for item in [softLeafLayer,leafLayer,softCenterLayer,centerLayer]{layer?.addSublayer(item);item.contentsGravity = .resizeAspect;item.contentsScale=2}
 }
 required init?(coder:NSCoder){fatalError("Programmatic artwork view")}
 override func hitTest(_ point:NSPoint)->NSView?{nil}
 override func layout(){super.layout();for item in [softLeafLayer,leafLayer,softCenterLayer,centerLayer]{item.frame=bounds}}
 func display(progress:Double,count:Int,pressed:Bool,shine:Double=1){
  let art=IrisArtwork.shared
  guard art.available else{return}
  let frame=min(72,max(0,Int((progress*72).rounded())))
  CATransaction.begin();CATransaction.setDisableActions(true)
  let strength=Float(ConverterModel.sanitizeAmount(shine,default:ConverterModel.defaultGlassShine,lo:0,hi:1))
  let emergence=Float(min(1,max(0,(progress-0.06)/0.17)))
  // Matched Blender endpoints keep opaque pigment and geometry below the
  // adjustable reflection. Slider playback only changes cached layer opacity.
  if art.adjustable{
   softLeafLayer.contents=count == 5 ? art.softFiveFrames[frame]:art.softFrames[frame]
   softLeafLayer.opacity=emergence
   softCenterLayer.contents=pressed ? art.softPressedCenter:art.softCenter
   softCenterLayer.opacity=1
  }else{softLeafLayer.opacity=0;softCenterLayer.opacity=0}
  leafLayer.contents=count == 5 ? art.fiveFrames[frame]:art.frames[frame]
  // Fade only the emergence of folded leaves; settled optical colour is 100%.
  leafLayer.opacity=emergence*(art.adjustable ? strength:1)
  centerLayer.contents=pressed ? art.pressedCenter:art.center
  centerLayer.opacity=art.adjustable ? strength:1
  CATransaction.commit()
 }
}

struct IrisRenderedBody:NSViewRepresentable {
 let progress:Double;let count:Int;let pressed:Bool
 var shine:Double=1
 func makeNSView(context:Context)->IrisRenderView{IrisRenderView(frame:.zero)}
 func updateNSView(_ view:IrisRenderView,context:Context){view.display(progress:progress,count:count,pressed:pressed,shine:shine)}
}
