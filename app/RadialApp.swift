import SwiftUI
import AppKit
import UniformTypeIdentifiers
import ScreenCaptureKit
import CoreGraphics
import Combine

enum WheelPhase:String {case idle,format,layout,review,converting,result}
struct WheelChoice:Identifiable {
 let id:String;let label:String;let icon:String;let subtitle:String;var enabled=true
}
@MainActor final class WheelFlow:ObservableObject {
 @Published var phase:WheelPhase = .idle
 @Published var revealID=0; @Published var revealing=false
 @Published var pressing=false
 @Published var hover:String?; @Published var selected:String?; @Published var message=""
 weak var presentationWindow:NSWindow?
 @Published var filePanel:NSOpenPanel?
 var panelObserver:((NSOpenPanel)->Void)?
 let model:ConverterModel
 init(_ model:ConverterModel){self.model=model}
 var formats:[WheelChoice] {[
  .init(id:"WAV",label:"WAV",icon:"waveform",subtitle:"PCM · 16/24/32-bit"),
  .init(id:"AIFF",label:"AIFF",icon:"waveform.path",subtitle:model.text("無壓縮 PCM")),
  .init(id:"FLAC",label:"FLAC",icon:"archivebox",subtitle:model.text("無損 · 最多 8 聲道")),
  .init(id:"OGG",label:"OGG",icon:"music.note",subtitle:model.text("Vorbis · 單／雙聲道")),
  .init(id:"MP3",label:"MP3",icon:"music.note.list",subtitle:model.text("有損 · 128–320 kbps"))
 ]}
 var audioJobs:[AudioJob]{model.jobs.filter{!$0.info.isEmpty}}
 var layouts:[WheelChoice] {
  let adm=audioJobs.contains{$0.info["adm"] as? Bool == true}
  let allADM=audioJobs.allSatisfy{$0.info["adm"] as? Bool == true}
  let knownSources=Set(["mono","stereo"]+UpmixLayoutCatalogue.sourceLayouts.map(\.id))
  let ordinaryOK=audioJobs.allSatisfy{$0.info["adm"] as? Bool == true || knownSources.contains($0.layout)}
  // Pure PCM stereo batch: the standard 5.1 target is gated off, so the
  // experimental upmix targets substitute. Any ADM in the batch disables
  // the upmix petals because the renderer is ADM-only. The wheel exposes
  // a 5.1 Upmix leaf (legacy v1 trial) and a 7.1.4 Upmix leaf (new v2);
  // the advanced popover lists every implemented catalogue layout.
  let pureStereo=audioJobs.allSatisfy{($0.info["adm"] as? Bool != true) && $0.layout == "stereo" && ($0.info["channels"] as? Int ?? 0) == 2}
  var options=[WheelChoice]()
  if !adm {options.append(.init(id:"same",label:model.text("保持原檔排列"),icon:"arrow.triangle.2.circlepath",subtitle:model.text("保留來源聲道排列")))}
  options += [
   .init(id:"stereo",label:model.text("雙聲道"),icon:"hifispeaker.2",subtitle:adm ? model.text("ADM → 雙聲道"):"PCM / LoRo",enabled:ordinaryOK)
  ]
  if pureStereo {
   // The legacy 5.1 trial (v1) leaf. Format gate stays as before.
   options.append(.init(id:"upmix-5.1",label:"5.1 Upmix",icon:"circle.grid.2x2",subtitle:model.text("試用 · 環境聲延伸"),enabled:model.format != "OGG"))
   // The new v2 wheel leaf maps to the most cinematic implemented
   // layout id (7.1.4-rear-first); the advanced popover can swap to any
   // of the other 9 implemented ids. 7.1.4 needs WAV/AIFF.
   let v2Leaf = UpmixLayoutCatalogue.v2WheelLeafLayout
   let leafEnabled = model.format != "OGG" && ["WAV","AIFF"].contains(model.format)
   options.append(.init(id:"upmix-"+v2Leaf.id,label:model.text("7.1.4 Upmix"),icon:"cube",subtitle:model.text("Upmix · \(v2Leaf.name)"),enabled:leafEnabled))
  } else {
   options.append(.init(id:"5.1-side",label:"5.1",icon:"circle.grid.2x2",subtitle:model.text("側環繞 · 6 聲道"),enabled:allADM && model.format != "OGG"))
   options += [
    .init(id:"7.1.4-rear-first",label:"7.1.4",icon:"cube",subtitle:model.text("12 聲道 · WAV/AIFF"),enabled:allADM && ["WAV","AIFF"].contains(model.format)),
   ]
  }
  options += [
   .init(id:"binaural",label:model.text("雙耳渲染"),icon:"headphones",subtitle:model.text("Apple HRTF · 雙聲道"),enabled:audioJobs.allSatisfy{$0.info["adm"] as? Bool == true || ["stereo","5.1-side","7.1.4-side-first","7.1.4-rear-first"].contains($0.layout)})
  ];return options.map{var item=$0;item.enabled=model.issue(for:item.id)==nil;return item}
 }
 var choices:[WheelChoice]{phase == .format || phase == .idle ? formats:layouts}
 var hovered:WheelChoice? {choices.first{$0.id == hover}}
 func choose(_ id:String) {
  guard !model.jobs.isEmpty,let choice=choices.first(where:{$0.id==id}),!model.running,!revealing,filePanel == nil,!model.choosingOutput else{return}
  guard choice.enabled else{message=model.issue(for:id) ?? "此格式不支援此聲道配置。";return}
  message=""
  if phase == .format {model.setFormat(id);phase = .layout;selected=nil;hover=nil}
  else if phase == .layout || phase == .review {
   if id=="upmix-5.1"{model.upmixLayout=UpmixLayoutCatalogue.v1TrialLayoutId}
   else if id=="upmix-"+UpmixLayoutCatalogue.v2WheelLeafLayoutId{model.upmixLayout=UpmixLayoutCatalogue.v2WheelLeafLayoutId}
   model.target=id;selected=id;phase = .review
  }
 }
 func chooseFiles(){
  guard !model.running,!model.choosingOutput,!revealing,filePanel == nil,let window=presentationWindow else{return}
  let panel=NSOpenPanel();panel.canChooseFiles=true;panel.canChooseDirectories=true;panel.allowsMultipleSelection=true;panel.isReleasedWhenClosed=false
  if let i=CommandLine.arguments.firstIndex(of:"--ui-fixture-directory"),i+1<CommandLine.arguments.count{panel.directoryURL=URL(fileURLWithPath:CommandLine.arguments[i+1])}
  panel.title=model.text("選擇音檔或資料夾");panel.prompt=model.text("加入")
  filePanel=panel
  panelObserver?(panel)
  panel.beginSheetModal(for:window){[weak self] response in
   guard let self else{return}
   let urls=response == .OK ? panel.urls:[]
   self.filePanel=nil
   // Cancel has no queue, phase or selection side effects.
   if !urls.isEmpty{Task{@MainActor in await self.accept(urls)}}
  }
 }
 func accept(_ urls:[URL],format:String?=nil) async {
  guard !model.running,!model.choosingOutput,!revealing else{return}
  let fresh=audioJobs.isEmpty || phase == .result
  let oldIDs=Set(model.jobs.map(\.id)),oldPhase=phase
  if fresh{model.jobs.removeAll();selected=nil;hover=nil;phase = .idle}
  message=""
  await model.add(urls)
  let added=model.jobs.filter{!oldIDs.contains($0.id)}
  if !audioJobs.isEmpty {
   phase = fresh ? .format:oldPhase
   message=added.contains{$0.info.isEmpty} ? "部分檔案未就緒；開啟明細查看原因。":""
   if !fresh,[.layout,.review].contains(oldPhase){phase = .layout;selected=nil;hover=nil}
   if fresh{
    revealing=true;revealID += 1;let generation=revealID
    let short=NSWorkspace.shared.accessibilityDisplayShouldReduceMotion || CommandLine.arguments.contains("--reduce-motion-test")
    try? await Task.sleep(nanoseconds:short ? 180_000_000:1_650_000_000)
    guard generation == revealID else{return}
    revealing=false
   }
   if let format{choose(format)}
  } else {phase = .idle;message="無法讀取音檔："+(added.first?.detail ?? "未找到支援的音檔")}
 }
 func begin(){
  guard phase == .review,!model.running,!model.choosingOutput,!revealing else{return}
  if let issue=model.issue(for:model.target){message=issue;return}
  if model.outDir.isEmpty{model.chooseOutput()}
  guard !model.outDir.isEmpty else{return}
  message="";phase = .converting;model.start()
 }
 func retry(){guard !model.running,!model.pendingJobs.isEmpty else{return};phase = .review;selected=model.target;hover=nil;message=model.issue(for:model.target) ?? "重試未完成項目；已完成輸出保留。"}
 func back(){hover=nil;selected=nil;phase = phase == .review ? .layout:.format}
 func cancel(){
  guard !model.jobs.isEmpty else{phase = .idle;return}
  if model.running{model.cancel();message="取消中…"}
  else{phase = .format;selected=nil;hover=nil;message="已取消選擇；原檔保留。"}
 }
 func reset(){if model.running{model.cancel()} else {model.jobs.removeAll();phase = .idle;hover=nil;selected=nil;message="";revealing=false;revealID += 1}}
 var targetLabel:String {
  if model.target.hasPrefix("upmix-"),let selected=UpmixLayoutCatalogue.layout(for:model.upmixLayout){return model.text(selected.name)}
  return layouts.first{$0.id==model.target}?.label ?? model.target
 }
 var readySummary:String {
  let base = "\(model.format) · \(targetLabel)"
  let trial = model.target.hasPrefix("upmix-") ? " · " + model.text("Upmix") : ""
  let sourceRateLabel = model.format == "MP3" ? model.text("來源取樣率（MP3 44.1／48 kHz）"):model.text("來源取樣率")
  return base + trial + " · \(model.rate == "0" ? sourceRateLabel:model.rate + " Hz")" + (model.format=="MP3" ? " · \(model.mp3Bitrate) kbps CBR":model.format=="OGG" ? " · Vorbis":model.depth=="float32" ? " · 32-bit float":" · \(model.depth)-bit")
}
}

/// Rounded annular petal, separated by radial gaps rather than a rectangular card.
struct WheelPetal:Shape {
 let index:Int;let count:Int
 func path(in rect:CGRect)->Path {
  let c=CGPoint(x:rect.midX,y:rect.midY),outer=Double(rect.width)*0.47,inner=Double(rect.width)*0.205
  let step=2*Double.pi/Double(count),center = -Double.pi/2+Double(index)*step
  let a=center-step/2+0.045,b=center+step/2-0.045,round=0.07
  func p(_ angle:Double,_ r:Double)->CGPoint{CGPoint(x:c.x+cos(angle)*r,y:c.y+sin(angle)*r)}
  var path=Path();path.move(to:p(a,inner+12));path.addLine(to:p(a,outer-13))
  path.addQuadCurve(to:p(a+round,outer),control:p(a,outer))
  path.addArc(center:c,radius:outer,startAngle:.radians(a+round),endAngle:.radians(b-round),clockwise:false)
  path.addQuadCurve(to:p(b,outer-13),control:p(b,outer));path.addLine(to:p(b,inner+12))
  path.addQuadCurve(to:p(b-round,inner),control:p(b,inner))
  path.addArc(center:c,radius:inner,startAngle:.radians(b-round),endAngle:.radians(a+round),clockwise:true)
  path.addQuadCurve(to:p(a,inner+12),control:p(a,inner));path.closeSubpath();return path
 }
}

/// Pointer light sits below live captions and never receives pointer input.
struct PetalHoverGlow:View {
 let petal:WheelPetal;let tint:Color;let highlighted:Bool;let reducedMotion:Bool
 var body:some View {
  ZStack {
   petal.stroke(tint.opacity(0.82),lineWidth:4).blur(radius:7)
   petal.fill(LinearGradient(colors:[.white.opacity(0.26),tint.opacity(0.12),.white.opacity(0.08)],startPoint:.topLeading,endPoint:.bottomTrailing))
   petal.stroke(LinearGradient(colors:[.white.opacity(0.95),tint.opacity(0.80)],startPoint:.topLeading,endPoint:.bottomTrailing),lineWidth:1.5)
    .shadow(color:tint.opacity(0.55),radius:8)
  }.opacity(highlighted ? 1:0)
   .animation(reducedMotion ? nil:.easeOut(duration:0.18),value:highlighted)
   .allowsHitTesting(false).accessibilityHidden(true)
 }
}

struct WheelHint:Equatable {let title:String;let detail:String;let warning:Bool}
struct WheelBubble<Content:View>:View {
 var width:CGFloat=320
 @ViewBuilder let content:Content
 var body:some View {
  VStack(spacing:0){
   content.padding(14).frame(width:width)
    .background(RoundedRectangle(cornerRadius:16).fill(Color(white:0.98)))
    .overlay(RoundedRectangle(cornerRadius:16).stroke(Color.black.opacity(0.12),lineWidth:0.5))
   Path{p in p.move(to:.zero);p.addLine(to:CGPoint(x:14,y:0));p.addLine(to:CGPoint(x:7,y:7));p.closeSubpath()}
    .fill(Color(white:0.98)).frame(width:14,height:7)
  }.foregroundStyle(Color(red:0.14,green:0.18,blue:0.20)).fixedSize().preferredColorScheme(.light)
 }
}
struct WheelHintBubble:View {
 let hint:WheelHint
 var body:some View {
  WheelBubble {
   HStack(alignment:.top,spacing:8){
    if hint.warning{Image(systemName:"exclamationmark.triangle.fill").foregroundStyle(Color.orange).font(.system(size:12))}
    VStack(alignment:.leading,spacing:4){
     if !hint.title.isEmpty{Text(hint.title).font(ConverterTypography.font(12,weight:.semibold))}
     Text(hint.detail).font(ConverterTypography.font(11)).fixedSize(horizontal:false,vertical:true)
    }.frame(maxWidth:.infinity,alignment:.leading)
   }
  }
 }
}

struct BadgeShape:InsettableShape {
 var insetAmount:CGFloat=0
 func inset(by amount:CGFloat)->BadgeShape{var copy=self;copy.insetAmount += amount;return copy}
 func path(in rect:CGRect)->Path{
  Circle().path(in:rect.insetBy(dx:insetAmount,dy:insetAmount))
 }
}
struct BadgePressStyle:ButtonStyle {
 @ObservedObject var flow:WheelFlow
 func makeBody(configuration:Configuration)->some View{configuration.label.onChange(of:configuration.isPressed){_,pressed in flow.pressing=pressed}}
}
extension View {
 @ViewBuilder func groupedGlass()->some View {
  if #available(macOS 26.0,*){GlassEffectContainer(spacing:0){self}} else{self}
 }
 @ViewBuilder func petalGlass(enabled:Bool,shape:WheelPetal)->some View {
  if #available(macOS 26.0,*),enabled{self.glassEffect(.clear,in:shape)}else{self}
 }
 @ViewBuilder func centerGlass(enabled:Bool)->some View {
  if #available(macOS 26.0,*),enabled{self.glassEffect(.regular,in:BadgeShape())}else{self}
 }
}
struct GlassPetal:View {
 let petal:WheelPetal;let tint:Color;let active:Bool;let enabled:Bool;let reduceTransparency:Bool
 var body:some View {
  ZStack{
   petal.fill(tint).offset(x:3,y:4)
   material
    .overlay(petal.fill(LinearGradient(colors:[Color.white.opacity(reduceTransparency ? 1:0.94),Color(red:0.97,green:0.98,blue:1).opacity(reduceTransparency ? 1:0.90)],startPoint:.topLeading,endPoint:.bottomTrailing)))
    .overlay(petal.fill(LinearGradient(colors:[tint.opacity(active ? 0.13:0.055),.clear,tint.opacity(0.04)],startPoint:.topLeading,endPoint:.bottomTrailing)))
    .overlay(petal.stroke(LinearGradient(colors:[.white.opacity(0.95),tint.opacity(active ? 0.85:0.28),.white.opacity(0.5)],startPoint:.topLeading,endPoint:.bottomTrailing),lineWidth:active ? 1.6:0.8))
  }.shadow(color:.black.opacity(0.12),radius:5,y:3)
   .shadow(color:tint.opacity(active ? 0.22:0),radius:active ? 5:0)
   .opacity(enabled ? 1:0.42).allowsHitTesting(false)
 }
 @ViewBuilder var material:some View {
  if reduceTransparency {petal.fill(.white)}
  else if #available(macOS 26.0,*) {petal.fill(Color.clear)}
  else {petal.fill(.ultraThinMaterial)}
 }
}

/// Milky front plate with offset Google-color backing; all decoration stays below input.
struct BeveledGlassBadge:View {
 let reduceTransparency:Bool
 var pressed=false
 private let colors=[Color(red:66/255,green:133/255,blue:244/255),Color(red:234/255,green:67/255,blue:53/255),Color(red:251/255,green:188/255,blue:5/255),Color(red:52/255,green:168/255,blue:83/255)]
 @ViewBuilder var body:some View {
  if let resources=Bundle.main.resourceURL,let normal=NSImage(contentsOf:resources.appendingPathComponent("ButtonAssets/normal-body.png")),let down=NSImage(contentsOf:resources.appendingPathComponent("ButtonAssets/pressed-body.png")){
   ZStack{Image(nsImage:normal).resizable().opacity(pressed ? 0:1);Image(nsImage:down).resizable().opacity(pressed ? 1:0)}.frame(width:178.6,height:178.6).allowsHitTesting(false)
  }else{nativeBody}
 }
 var nativeBody:some View {
  ZStack {
   BadgeShape().fill(AngularGradient(colors:colors+[colors[0]],center:.center,startAngle:.degrees(-90),endAngle:.degrees(270))).offset(x:4,y:5)
   material
    .overlay(BadgeShape().inset(by:0.8).fill(LinearGradient(colors:[Color.white.opacity(reduceTransparency ? 1:0.96),Color(red:0.96,green:0.98,blue:1).opacity(reduceTransparency ? 1:0.94)],startPoint:.topLeading,endPoint:.bottomTrailing)))
    .overlay(BadgeShape().inset(by:7).fill(Color.white.opacity(0.18)))
    .overlay(BadgeShape().strokeBorder(LinearGradient(colors:[.white.opacity(0.98),Color(white:0.65).opacity(0.22),.white.opacity(0.85)],startPoint:.topLeading,endPoint:.bottomTrailing),lineWidth:0.85))
    .overlay(BadgeShape().inset(by:2).stroke(LinearGradient(colors:[.white.opacity(0.75),.clear,Color(white:0.6).opacity(0.15)],startPoint:.top,endPoint:.bottom),lineWidth:0.5))
  }.shadow(color:.black.opacity(0.12),radius:5,y:3).allowsHitTesting(false)
 }
 @ViewBuilder var material:some View {
  if reduceTransparency{BadgeShape().fill(.white)}
  else if #available(macOS 26.0,*){BadgeShape().fill(Color.clear)}
  else{BadgeShape().fill(.ultraThinMaterial)}
 }
}
struct EngravedDropLabel:View {
 let label:String
 var body:some View {
  ZStack{
   Text(label)
  }.font(ConverterTypography.font(15,weight:.semibold))
   .accessibilityElement(children:.ignore).accessibilityLabel(label)
 }
}

@MainActor func droppedFileURLs(_ pasteboard:NSPasteboard)->[URL]{
 if let urls=pasteboard.readObjects(forClasses:[NSURL.self],options:[.urlReadingFileURLsOnly:true]) as? [URL],!urls.isEmpty{return urls.filter(\.isFileURL)}
 if let paths=pasteboard.propertyList(forType:.init("NSFilenamesPboardType")) as? [String]{return paths.map{URL(fileURLWithPath:$0)}}
 return []
}
@MainActor func recordDropEvent(_ name:String,_ sender:NSDraggingInfo,_ flow:WheelFlow?){
 guard let i=CommandLine.arguments.firstIndex(of:"--ui-event-log"),i+1<CommandLine.arguments.count else{return}
 let path=CommandLine.arguments[i+1],entry:[String:Any]=["event":name,"time":Date().timeIntervalSince1970,"url_count":droppedFileURLs(sender.draggingPasteboard).count,"types":sender.draggingPasteboard.types?.map(\.rawValue) ?? [],"phase":flow?.phase.rawValue ?? "","running":flow?.model.running ?? false,"revealing":flow?.revealing ?? false]
 guard var data=try? JSONSerialization.data(withJSONObject:entry) else{return};data.append(10)
 if !FileManager.default.fileExists(atPath:path){FileManager.default.createFile(atPath:path,contents:nil)}
  if let file=FileHandle(forWritingAtPath:path){_ = try? file.seekToEnd();try? file.write(contentsOf:data);try? file.close()}
}
/// The separate child panel keeps every caption and tool inside the bubble.
class WheelInfoPanel:NSPanel {
 override var canBecomeKey:Bool{true}
 override var canBecomeMain:Bool{false}
}
/// Input lives above the GlassEffectContainer, which owns native glass subviews.
@MainActor final class WheelInteractionView:NSView {
 var flow:WheelFlow!
 var bubbleContent:AnyView?
 private var hintWindow:NSPanel?
 private weak var hintParent:NSWindow?
 var pressed:String?;var downEvent:NSEvent?
 private var downOrigin=NSPoint.zero;private var downPointer=NSPoint.zero;private var dragged=false
 override var isFlipped:Bool{true}
 override init(frame:NSRect){super.init(frame:frame);registerForDraggedTypes([.fileURL,.URL,.init("NSFilenamesPboardType")])}
 required init?(coder:NSCoder){fatalError("unused")}
 override func viewWillMove(toWindow newWindow:NSWindow?){
  if newWindow == nil{hideHint();hintWindow?.close();hintWindow=nil}
  super.viewWillMove(toWindow:newWindow)
 }
 var currentHint:WheelHint? {
  guard let flow,!flow.model.running,!flow.revealing,flow.filePanel == nil,!flow.model.choosingOutput else{return nil}
  if [.format,.layout,.review].contains(flow.phase),let choice=flow.hovered{
   return WheelHint(title:choice.label,detail:choice.enabled ? choice.subtitle:(flow.model.issue(for:choice.id) ?? choice.subtitle),warning:!choice.enabled)
  }
  if !flow.message.isEmpty{return WheelHint(title:"",detail:flow.model.text(flow.message),warning:true)}
  return nil
 }
 private func hideHint(){
  if let hintWindow{hintParent?.removeChildWindow(hintWindow);hintWindow.orderOut(nil)}
  hintParent=nil
 }
 func updateHint(){
  guard let bubbleContent,let parent=window,parent.isVisible,flow.filePanel == nil,!flow.model.choosingOutput else{hideHint();return}
  let panel: NSPanel
  if let existing=hintWindow{panel=existing}else{
   panel=WheelInfoPanel(contentRect:.zero,styleMask:[.borderless,.nonactivatingPanel],backing:.buffered,defer:false)
   panel.isReleasedWhenClosed=false;panel.isOpaque=false;panel.backgroundColor = .clear
   panel.hasShadow=true;panel.isFloatingPanel=true;panel.becomesKeyOnlyIfNeeded=true;panel.hidesOnDeactivate=false
   panel.level=parent.level;panel.title="UTUVO convert Info";hintWindow=panel
  }
  let content:NSHostingView<AnyView>
  if let existing=panel.contentView as? NSHostingView<AnyView>{content=existing;content.rootView=bubbleContent}
  else{content=NSHostingView(rootView:bubbleContent);panel.contentView=content}
  panel.setContentSize(content.fittingSize)
  let screen=parent.screen?.visibleFrame ?? parent.frame.insetBy(dx:-300,dy:-300)
  let size=panel.frame.size
  let x=min(max(parent.frame.midX-size.width/2,screen.minX+8),screen.maxX-size.width-8)
  let y=min(parent.frame.maxY+8,screen.maxY-size.height-8)
  panel.setFrameOrigin(NSPoint(x:x,y:max(y,screen.minY+8)))
  if hintParent !== parent{hideHint();parent.addChildWindow(panel,ordered:.above);hintParent=parent}
  if !panel.isVisible{panel.orderFront(nil)}
 }
 override func acceptsFirstMouse(for event:NSEvent?)->Bool{true}
 override var mouseDownCanMoveWindow:Bool{false}
 func region(_ point:NSPoint)->String?{
  guard flow.filePanel == nil,!flow.model.choosingOutput else{return nil}
  let dx=point.x-bounds.midX,dy=point.y-bounds.midY,r=hypot(dx,dy)
  if flow.model.analysing{return r<=81 ? "move":nil}
  if r<=65 || (flow.phase == .idle && r<=81){
   if flow.phase == .idle{return r>48 ? "move":"file"}
   if flow.phase == .review,r<=58{return "convert"}
   return "move"
  }
  guard [.format,.layout,.review].contains(flow.phase),!flow.model.running,!flow.revealing else{return nil}
  return flow.choices.enumerated().first{WheelPetal(index:$0.offset,count:flow.choices.count).path(in:bounds).contains(point)}.map{"choice:"+$0.element.id}
 }
 override func hitTest(_ point:NSPoint)->NSView?{region(convert(point,from:superview)) == nil ? nil:self}
 override func mouseDown(with event:NSEvent){
  let p=convert(event.locationInWindow,from:nil);pressed=region(p);traceInput("down",p,event)
  downOrigin=window?.frame.origin ?? .zero;downPointer=wheelScreenPointer(event,window:window);dragged=false
  downEvent=event;let press=["file","convert"].contains(pressed ?? "");if flow.pressing != press{flow.pressing=press}
 }
 override func mouseDragged(with event:NSEvent){
  guard downEvent != nil,pressed != nil else{return}
  let p=wheelScreenPointer(event,window:window),dx=p.x-downPointer.x,dy=p.y-downPointer.y
  if hypot(dx,dy)>=4{dragged=true}
  guard dragged else{return}
  flow.pressing=false;window?.setFrameOrigin(NSPoint(x:downOrigin.x+dx,y:downOrigin.y+dy))
 }
 func traceInput(_ name:String,_ p:NSPoint,_ event:NSEvent){
  guard let i=CommandLine.arguments.firstIndex(of:"--ui-event-log"),i+1<CommandLine.arguments.count else{return}
  let record:[String:Any]=["event":"pointer-"+name,"action":pressed ?? "none","x":p.x,"y":p.y,"bounds":NSStringFromRect(bounds),"frame":NSStringFromRect(frame),"window_frame":NSStringFromRect(window?.frame ?? .zero)]
  if var data=try? JSONSerialization.data(withJSONObject:record){data.append(10);if let f=FileHandle(forWritingAtPath:CommandLine.arguments[i+1]){_ = try? f.seekToEnd();try? f.write(contentsOf:data);try? f.close()}}
 }
 override func mouseUp(with event:NSEvent){
  traceInput("up",convert(event.locationInWindow,from:nil),event)
  let p=wheelScreenPointer(event,window:window),moved=dragged || hypot(p.x-downPointer.x,p.y-downPointer.y)>=4
  let action=pressed;pressed=nil;downEvent=nil;dragged=false;flow.pressing=false
  if moved{window?.setFrameOrigin(NSPoint(x:downOrigin.x+p.x-downPointer.x,y:downOrigin.y+p.y-downPointer.y));return}
  guard action==region(convert(event.locationInWindow,from:nil)) else{return}
  performClick(action)
 }
 func performClick(_ action:String?){
  switch action{case "file":flow.chooseFiles();case "convert":flow.begin();case .some(let id) where id.hasPrefix("choice:"):flow.choose(String(id.dropFirst(7)));default:break}
 }
 override func updateTrackingAreas(){super.updateTrackingAreas();for area in trackingAreas{removeTrackingArea(area)};addTrackingArea(NSTrackingArea(rect:bounds,options:[.mouseMoved,.mouseEnteredAndExited,.activeAlways],owner:self,userInfo:nil))}
 override func mouseEntered(with event:NSEvent){mouseMoved(with:event)}
 override func mouseMoved(with event:NSEvent){let id=region(convert(event.locationInWindow,from:nil));flow.hover=id?.hasPrefix("choice:") == true ? String(id!.dropFirst(7)):nil;updateHint()}
 override func mouseExited(with event:NSEvent){flow.hover=nil;updateHint()}
 override func draggingEntered(_ sender:NSDraggingInfo)->NSDragOperation{recordDropEvent("surface-enter",sender,flow);guard !flow.model.running,!flow.model.choosingOutput,!flow.revealing,flow.filePanel==nil else{return []};return droppedFileURLs(sender.draggingPasteboard).isEmpty ? []:.copy}
 override func draggingUpdated(_ sender:NSDraggingInfo)->NSDragOperation{draggingEntered(sender)}
 override func draggingExited(_ sender:NSDraggingInfo?){if let sender{recordDropEvent("surface-exit",sender,flow)}}
 override func prepareForDragOperation(_ sender:NSDraggingInfo)->Bool{recordDropEvent("surface-prepare",sender,flow);return draggingEntered(sender) == .copy}
 override func performDragOperation(_ sender:NSDraggingInfo)->Bool{recordDropEvent("surface-perform",sender,flow);let urls=droppedFileURLs(sender.draggingPasteboard);guard !flow.model.running,!flow.model.choosingOutput,!flow.revealing,flow.filePanel==nil,!urls.isEmpty else{return false};Task{await flow.accept(urls)};return true}
}
struct WheelInteractionSurface:NSViewRepresentable {
 @ObservedObject var flow:WheelFlow
 let bubbleContent:AnyView?
 func makeNSView(context:Context)->WheelInteractionView{let view=WheelInteractionView();view.flow=flow;return view}
 func updateNSView(_ view:WheelInteractionView,context:Context){view.flow=flow;view.bubbleContent=bubbleContent;view.updateHint()}
}
enum WheelPresentation:Equatable {
 case body,expanded,compact
 var size:NSSize{switch self{case .body,.compact:NSSize(width:180,height:180);case .expanded:NSSize(width:420,height:360)}}
 // Window coordinates of the body centre, not the screen or live mouse.
 var bodyCentre:NSPoint{switch self{case .body,.compact:NSPoint(x:90,y:90);case .expanded:NSPoint(x:210,y:180)}}
 func resize(_ window:NSWindow,from previous:WheelPresentation){
  let origin=window.frame.origin,old=previous.bodyCentre,new=bodyCentre
  window.setFrame(NSRect(x:origin.x+old.x-new.x,y:origin.y+old.y-new.y,width:size.width,height:size.height),display:true,animate:false)
 }
}
/// A scan stays inside the central glass circle and ends with the real probe.
struct AnalysisCircle:View {
 let label:String
 let reducedMotion:Bool
 static func angle(at date:Date)->Double{date.timeIntervalSinceReferenceDate.truncatingRemainder(dividingBy:1.6)/1.6*360}
 var body:some View {
  TimelineView(.animation(minimumInterval:1.0/30,paused:reducedMotion)){context in
   ZStack{
    Circle().stroke(Color(red:0.25,green:0.46,blue:0.66).opacity(0.14),lineWidth:2)
    Circle().trim(from:0,to:0.28)
     .stroke(AngularGradient(colors:[.clear,Color(red:0.26,green:0.54,blue:0.77)],center:.center),style:StrokeStyle(lineWidth:3,lineCap:.round))
     .rotationEffect(.degrees(reducedMotion ? -90:Self.angle(at:context.date)))
    VStack(spacing:6){
     Image(systemName:"waveform").font(.system(size:21,weight:.medium)).foregroundStyle(Color(red:0.27,green:0.49,blue:0.64))
     Text(label).font(ConverterTypography.font(12,weight:.semibold))
    }
   }.frame(width:110,height:110)
  }.allowsHitTesting(false).accessibilityElement(children:.ignore)
   .accessibilityLabel(label).accessibilityIdentifier("analysis-circle")
 }
}
struct RadialView:View {
 @ObservedObject var model:ConverterModel;@ObservedObject var flow:WheelFlow
 @Environment(\.accessibilityReduceMotion) private var reduceMotion
 @Environment(\.accessibilityReduceTransparency) private var reduceTransparency
 @StateObject private var iris=IrisMotion()
 @State private var irisCount=5
 @State private var dropping=false;@State private var advanced=false;@State private var batch=false
 private var compact:Bool{flow.phase == .converting || flow.phase == .result}
 private var presentation:WheelPresentation{model.analysing || (flow.phase == .idle && iris.progress==0) ? .body:compact ? .compact:.expanded}
 private let orange=Color(red:1,green:0.34,blue:0.08)
 var body:some View {
  Group{
   if presentation == .body{
    wheel.frame(width:180,height:180)
   }else{expandedBody}
  }.frame(width:presentation.size.width,height:presentation.size.height,alignment:.top)
   .foregroundStyle(Color(red:0.22,green:0.27,blue:0.28)).preferredColorScheme(.light)
   .background(Color.clear).font(ConverterTypography.font(12))
   .onDrop(of:[UTType.fileURL.identifier],isTargeted:$dropping){providers in receive(providers)}
   .onChange(of:model.running){_,running in if !running && flow.phase == .converting{flow.message="";flow.phase = .result}}
   .onChange(of:model.analysing){_,active in
    flow.pressing=false;irisCount=flow.choices.count
    iris.setOpen(!active && ![.idle,.converting,.result].contains(flow.phase),reduced:motionReduced)
   }
   .onChange(of:flow.phase){_,phase in
    flow.pressing=false
    if [.format,.layout,.review].contains(phase){irisCount=flow.choices.count}
    iris.setOpen(!model.analysing && ![.idle,.converting,.result].contains(phase),reduced:motionReduced)
   }
   .onChange(of:presentation){old,new in if let window=flow.presentationWindow{new.resize(window,from:old)}}
   .onChange(of:flow.revealID){_,_ in iris.setOpen(flow.phase != .idle,reduced:motionReduced)}
   .onExitCommand{if let panel=flow.filePanel{panel.cancel(nil)}else{flow.cancel()}}
 }
 private var wheel:some View {
  ZStack {
   if IrisArtwork.shared.available && !transparencyReduced {
    IrisRenderedBody(progress:model.analysing ? 0:iris.progress,count:irisCount,pressed:flow.pressing,shine:model.glassShine).frame(width:360,height:360).allowsHitTesting(false)
   }
   if flow.phase != .idle && !model.analysing {ForEach(Array(flow.choices.enumerated()),id:\.element.id){index,choice in petalButton(index:index,choice:choice)}}
   center.buttonStyle(BadgePressStyle(flow:flow)).glassInlaidInk().frame(width:130,height:130)
    .background{if transparencyReduced || !IrisArtwork.shared.available{BeveledGlassBadge(reduceTransparency:transparencyReduced,pressed:flow.pressing)}}
    .animation(.easeOut(duration:motionReduced ? 0:0.10),value:flow.pressing)
  }.frame(width:360,height:360)
   .overlay{WheelInteractionSurface(flow:flow,bubbleContent:showsInformation ? AnyView(informationBubble):nil).frame(width:360,height:360)}
 }
 private var expandedBody:some View {
  wheel.scaleEffect(compact ? 0.46:(dropping ? 1.035:1))
   .frame(width:presentation.size.width,height:presentation.size.height)
   .animation(.easeOut(duration:0.16),value:dropping)
 }
 var showsInformation:Bool{!model.analysing && (flow.phase != .idle || !model.jobs.isEmpty || !flow.message.isEmpty)}
 var informationBubble:some View {
  WheelBubble(width:advanced || batch ? 380:320) {
   if advanced{
    ScrollView{advancedView.padding(2)}.frame(height:model.target.hasPrefix("upmix-") ? 400:210)
   }else if batch{
    batchView.frame(height:min(360,CGFloat(72+model.jobs.count*148)))
   }else{
   VStack(alignment:.leading,spacing:10){
   HStack(spacing:14){
    Text("UTUVO CONVERT").font(.system(size:9,weight:.semibold)).tracking(1.5).foregroundStyle(.secondary)
    Spacer()
    Button{flow.reset()}label:{Image(systemName:"xmark").font(.system(size:11,weight:.semibold)).frame(width:20,height:20)}.buttonStyle(.plain).accessibilityLabel(model.text(model.running ? "取消處理":"清除選擇")).accessibilityIdentifier("clear-selection")
   }
   informationDescription
   VStack(alignment:.leading,spacing:7) {
    if flow.phase != .idle {
     if flow.phase == .review{Text(flow.readySummary).font(ConverterTypography.font(12,weight:.medium)).fixedSize(horizontal:false,vertical:true)}
     Text(model.jobs.count == 1 ? model.jobs[0].name:model.formatText("{0} 個檔案",String(model.jobs.count))).font(ConverterTypography.font(11)).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
    }
    if flow.phase == .review {
     Button{model.chooseOutput()}label:{Label(model.outDir.isEmpty ? model.text("選擇輸出目錄"):URL(fileURLWithPath:model.outDir).lastPathComponent,systemImage:"folder").lineLimit(1).truncationMode(.middle)}.buttonStyle(.plain).font(ConverterTypography.font(11)).accessibilityIdentifier("output-folder")
     Text(model.text("不做正規化 · 保留原檔")+(flow.audioJobs.contains{$0.info["adm"] as? Bool == true} ? " · ADM → PCM":"")).font(ConverterTypography.font(10)).foregroundStyle(.secondary).fixedSize(horizontal:false,vertical:true)
    }
    if flow.phase == .converting {
     ProgressView(value:model.overall).tint(orange)
     HStack{Text(model.jobs.first{$0.status=="轉換中"}?.name ?? model.text("處理中…")).lineLimit(1);Spacer();Button(model.text("取消"),action:flow.cancel).keyboardShortcut(.escape,modifiers:[])}.font(.caption)
    }
    if flow.phase == .result {
     let successful=model.jobs.filter{$0.output != nil}.count
     Text(model.resultSummary+(successful>0 ? " · QC PASS":"")).font(.caption).fixedSize(horizontal:false,vertical:true)
     HStack{if let out=model.jobs.first(where:{$0.output != nil})?.output{Button("Finder"){model.reveal(out)}.accessibilityIdentifier("reveal-output")};Button(model.text("明細")){batch=true}.accessibilityIdentifier("batch-details");if !model.pendingJobs.isEmpty{Button(model.text("重試未完成"),action:flow.retry).accessibilityIdentifier("retry-incomplete")};Button(model.text("再轉一批"),action:flow.reset).accessibilityIdentifier("start-new-batch")}.buttonStyle(.plain).font(ConverterTypography.font(11))
    }
   }.id(model.language)
   if !model.jobs.isEmpty{
   Divider()
   HStack(spacing:18){
    if [.layout,.review].contains(flow.phase) && !model.running {Button(model.text("返回"),action:flow.back)}
    Button{flow.chooseFiles()}label:{Image(systemName:"plus")}.accessibilityLabel(model.text("加入音檔")).keyboardShortcut("o",modifiers:.command).disabled(model.running || flow.revealing)
    Button(model.text("進階")){advanced=true}.disabled(model.running).accessibilityIdentifier("output-settings")
    Button(model.formatText("{0} 個檔案",String(model.jobs.count))){batch=true}.disabled(model.jobs.isEmpty).accessibilityIdentifier("batch-files")
    Spacer(minLength:0)
   }.buttonStyle(.plain).font(ConverterTypography.font(11)).padding(.vertical,2)
   }
   }
   }
  }.onExitCommand{advanced=false;batch=false}
 }
 @ViewBuilder private var informationDescription:some View {
  if !flow.message.isEmpty{
   Label(model.text(flow.message),systemImage:"exclamationmark.triangle.fill").font(ConverterTypography.font(11)).fixedSize(horizontal:false,vertical:true)
  }
  if [.format,.layout,.review].contains(flow.phase),!model.running,!flow.revealing,let choice=flow.hovered{
   VStack(alignment:.leading,spacing:4){
    Text(choice.label).font(ConverterTypography.font(13,weight:.semibold))
    Text(choice.enabled ? choice.subtitle:(model.issue(for:choice.id) ?? choice.subtitle)).font(ConverterTypography.font(11)).fixedSize(horizontal:false,vertical:true)
   }
  }else if flow.phase == .format{
   Text(model.text("1 · 選擇輸出格式")).font(ConverterTypography.font(13,weight:.semibold))
  }else if flow.phase == .layout{
   Text(model.text("2 · 選擇聲道排列／渲染")).font(ConverterTypography.font(13,weight:.semibold))
  }
 }
 @ViewBuilder func petalButton(index:Int,choice:WheelChoice)->some View {
     let petal=WheelPetal(index:index,count:flow.choices.count)
     let selected=flow.selected == choice.id
     let illuminated=flow.hover == choice.id && choice.enabled && !flow.revealing && !model.running && [.format,.layout,.review].contains(flow.phase)
     Button{flow.choose(choice.id)}label:{
      ZStack {
       if transparencyReduced || !IrisArtwork.shared.available {
        petal.fill(petalColor(index).opacity(choice.enabled ? 1:0.45))
       }
       if !choice.enabled {petal.fill(.black.opacity(0.38))}
       PetalHoverGlow(petal:petal,tint:petalColor(index),highlighted:illuminated,reducedMotion:motionReduced)
        let angle = -Double.pi/2+Double(index)*2*Double.pi/Double(flow.choices.count)
        Group {
         if IrisArtwork.shared.available && !transparencyReduced {
          VStack(spacing:6){
           Image(systemName:choice.icon).font(.system(size:20,weight:.medium))
           Text(choice.label).font(ConverterTypography.font(12,weight:.semibold))
          }.glassInlaidInk(light:index == 1).opacity(choice.enabled ? 1:0.60)
         } else {
          VStack(spacing:6){Image(systemName:choice.icon).font(.system(size:20,weight:.medium));Text(choice.label).font(ConverterTypography.font(12,weight:.semibold))}
           .glassInlaidInk().opacity(choice.enabled ? 1:0.60)
         }
        }
         .overlay(alignment:.bottom){if selected {Circle().fill(Color.white).overlay(Circle().stroke(petalColor(index),lineWidth:1)).frame(width:4,height:4).offset(y:7)}}
         .opacity(flow.hover == choice.id ? 1:0.94)
         .position(x:180+cos(angle)*121,y:180+sin(angle)*121)
      }
     }.buttonStyle(.plain)
      .opacity(min(1,max(0,(iris.progress-0.72)/0.23)))
      .frame(width:360,height:360).contentShape(petal).allowsHitTesting(false)
      .disabled(!choice.enabled || model.running || flow.revealing || flow.phase == .idle || [.converting,.result].contains(flow.phase))
      .keyboardShortcut(KeyEquivalent(Character(String(index+1))),modifiers:[])
      .accessibilityIdentifier("choice-"+choice.id)
      .accessibilityAddTraits(selected ? .isSelected:[])
      .accessibilityLabel(choice.label+". "+choice.subtitle+(choice.enabled ? "":". "+model.text("無法使用")))
 }
 @ViewBuilder var center:some View {
  VStack(spacing:8){
   if model.analysing {
    AnalysisCircle(label:model.text("分析中"),reducedMotion:motionReduced)
   } else if flow.phase == .format {
    Text(model.jobs.first?.info["adm"] as? Bool == true ? "ADM":model.formatText("{0} 個音檔",String(model.jobs.count))).font(.system(size:8,weight:.medium)).tracking(1.2)
    Text(model.text("選格式")).font(ConverterTypography.font(15,weight:.semibold)).tracking(0.5)
   } else if flow.phase == .idle {
    Button(action:flow.chooseFiles){EngravedDropLabel(label:model.text("拖入音檔"))}.accessibilityIdentifier("drop-audio").disabled(model.running).keyboardShortcut("o",modifiers:.command)
   } else if flow.phase == .layout {
    Text(model.format).font(.system(size:23,weight:.semibold));Image(systemName:"arrow.down").foregroundStyle(.secondary)
   } else if flow.phase == .review {
    Button(action:flow.begin){VStack(spacing:6){Image(systemName:"arrow.right").font(.title2);Text(model.text("轉換")).font(ConverterTypography.font(11,weight:.semibold)).tracking(1)}}.accessibilityIdentifier("convert").frame(width:105,height:105).disabled(model.issue(for:model.target) != nil).keyboardShortcut(.return,modifiers:[])
   } else if flow.phase == .converting {Text("\(Int(model.overall*100))%").font(.system(size:26,weight:.light,design:.rounded));Text(model.text("轉換中")).font(.system(size:8)).tracking(1)
   } else {Image(systemName:model.jobs.allSatisfy{$0.output != nil} ? "checkmark":model.jobs.contains{$0.status=="已取消"} ? "pause":"exclamationmark").font(.system(size:30,weight:.light));Text(model.text("結果")).font(.system(size:9)).tracking(1)}
  }
 }
 var transparencyReduced:Bool{reduceTransparency || CommandLine.arguments.contains("--reduce-transparency-test")}
 var motionReduced:Bool{reduceMotion || CommandLine.arguments.contains("--reduce-motion-test")}
 func petalColor(_ index:Int)->Color {
  // Opaque accessibility fallback keeps the same hues with enough lightness for dark ink.
  let colors=[Color(red:0.41,green:0.62,blue:0.97),Color(red:0.94,green:0.45,blue:0.41),Color(red:0.99,green:0.79,blue:0.22),Color(red:0.36,green:0.73,blue:0.46),Color(red:0.72,green:0.77,blue:0.82)]
  return colors[index % colors.count]
 }
 var advancedView:some View {
  VStack(alignment:.leading,spacing:14){HStack{Text(model.text("輸出設定")).font(.headline);Spacer();Button(model.text("完成")){advanced=false}.buttonStyle(.plain)}
   Picker(model.text("取樣率"),selection:$model.rate){Text(model.text(model.format == "MP3" ? "來源取樣率（MP3 44.1／48 kHz）":"來源取樣率")).tag("0");ForEach(model.availableRates,id:\.self){Text($0+" Hz").tag($0)}}
   if model.format == "MP3"{
    Picker(model.text("MP3 位元率"),selection:$model.mp3Bitrate){ForEach([128,192,256,320],id:\.self){Text("\($0) kbps").tag($0)}}.accessibilityIdentifier("mp3-bitrate")
    Text(model.text("MP3 · 有損編碼 · 單／雙聲道")).font(.caption).foregroundStyle(.secondary)
    Text(model.text("來源為其他取樣率時，MP3 會轉為 48 kHz。")).font(.caption).foregroundStyle(.secondary)
   }else if !model.lossyFormat{Picker(model.text("PCM 位深"),selection:$model.depth){ForEach(model.availableDepths,id:\.self){Text($0=="float32" ? "32-bit float":$0+"-bit").tag($0)}}}else{Text(model.text("OGG Vorbis · 有損編碼 · 單／雙聲道")).font(.caption).foregroundStyle(.secondary)}
   let isUpmix = model.target == "upmix-5.1" || model.target.hasPrefix("upmix-")
   if isUpmix {
    // Implemented Upmix layouts. The direct petals seed 5.1 or 7.1.4;
    // the advanced picker then selects the actual generic-v2 output.
    // The catalogue never infers a layout from channel count.
    let currentLayout = UpmixLayoutCatalogue.layout(for: model.upmixLayout)
    Menu {
     ForEach(UpmixLayoutCatalogue.implemented) { layout in
      Button {
       model.upmixLayout = layout.id
      } label: {
       Text(model.text(layout.name) + " · \(layout.channels) ch")
      }
      .disabled(model.running)
     }
     Divider()
     ForEach(UpmixLayoutCatalogue.reference) { entry in
      Button {} label: {
       Text(model.text(entry.name) + " · \(entry.kind) · " + model.text("未支援"))
      }
      .disabled(true)
     }
    } label: {
     HStack {
      Text(model.text("Upmix 排列"))
      Spacer()
      if let currentLayout {
       Text(model.text(currentLayout.name) + " · \(currentLayout.channels) ch")
      } else {
       Text(model.text("未支援"))
      }
     }
    }
    .disabled(model.running)
    .accessibilityIdentifier("upmix-layout")
    VStack(alignment:.leading,spacing:6){
     HStack{
      Text(model.text("Upmix 環繞"))
      Spacer()
      Text(String(format:"%.2f",model.upmixSurround)).monospacedDigit()
     }.font(.caption)
     Slider(value:$model.upmixSurround, in:0.0...1.0, step:0.05)
      .disabled(model.running)
      .accessibilityIdentifier("upmix-surround")
    }
    VStack(alignment:.leading,spacing:6){
     HStack{
      Text(model.text("Upmix 中央"))
      Spacer()
      Text(String(format:"%.2f",model.upmixCentre)).monospacedDigit()
     }.font(.caption)
     if let layout = currentLayout, !layout.hasCentre {
      Slider(value: .constant(0.0), in: 0.0...0.7)
       .disabled(true)
       .accessibilityIdentifier("upmix-centre-disabled")
      Text(model.text("Upmix 中央對此排列無效")).font(.system(size:10)).foregroundStyle(.secondary)
     } else {
      Slider(value:$model.upmixCentre, in:0.0...0.7, step:0.05)
       .disabled(model.running)
       .accessibilityIdentifier("upmix-centre")
     }
    }
    VStack(alignment:.leading,spacing:6){
     HStack{
      Text(model.text("Upmix 高度"))
      Spacer()
      Text(String(format:"%.2f",model.upmixHeight)).monospacedDigit()
     }.font(.caption)
     if let layout = currentLayout, !layout.hasHeight {
      Slider(value: .constant(0.0), in: 0.0...0.5)
       .disabled(true)
       .accessibilityIdentifier("upmix-height-disabled")
      Text(model.text("Upmix 高度對此排列無效")).font(.system(size:10)).foregroundStyle(.secondary)
     } else {
      Slider(value:$model.upmixHeight, in:0.0...0.5, step:0.05)
       .disabled(model.running)
       .accessibilityIdentifier("upmix-height")
     }
    }
    Text(model.text("此試用為估測之環境聲與中央聲，非原始環繞還原。")).font(.caption).foregroundStyle(.secondary)
   }
   Button{model.chooseOutput()}label:{Label(model.outDir.isEmpty ? model.text("輸出資料夾"):URL(fileURLWithPath:model.outDir).lastPathComponent,systemImage:"folder")}
   Text(model.text("預設保留來源取樣率。不做正規化；降位深未加 dither。輸出附聲道與 QC 報告，原始 metadata 不複製。")).font(.caption).foregroundStyle(.secondary)
  }
 }
 var batchView:some View {
  VStack(alignment:.leading,spacing:10){HStack{Text(model.text("批次明細")).font(.headline);Spacer();Button(model.text("完成")){batch=false}.buttonStyle(.plain)};ScrollView{VStack(alignment:.leading,spacing:12){ForEach($model.jobs){$job in
   VStack(alignment:.leading,spacing:5){HStack{Text(job.name).font(.caption).lineLimit(1);Spacer();Text(model.statusText(job.status)).font(.caption).foregroundStyle(.secondary);if !model.running{Button{model.remove(job.id);if model.jobs.isEmpty{flow.reset();batch=false}}label:{Image(systemName:"xmark.circle")}.buttonStyle(.plain).accessibilityLabel(model.formatText("移除 {0}",job.name))}};Text(model.detailText(job.detail)).font(.system(size:11)).foregroundStyle(.secondary).textSelection(.enabled)
    if job.status=="轉換中"{ProgressView(value:job.progress)}
    if let output=job.output{Button(model.text("在 Finder 顯示輸出")){model.reveal(output)}.font(.caption)}
    if job.info["adm"] as? Bool != true && !job.info.isEmpty {
     if job.info["layout"] as? String != "unknown"{Text(model.text("來源聲道排列")+" · "+job.layout).font(.caption).foregroundStyle(.secondary)}
     else{Picker(model.text("來源聲道排列"),selection:$job.layout){Text(model.text("未指定")).tag("unknown");ForEach(sourceLayouts(job),id:\.self){Text(sourceLayoutName($0)).tag($0)}}.disabled(model.running || job.output != nil)}
    }
    if let summary=job.info["summary"] as? [String:Any],let programmes=summary["programmes"] as? [[String:String]],programmes.count>1 {Picker(model.text("ADM 節目"),selection:$job.programme){Text(model.text("選擇節目")).tag("");ForEach(0..<programmes.count,id:\.self){i in Text(programmes[i]["name"] ?? "").tag(programmes[i]["id"] ?? "")}}.disabled(model.running)}
   }
    Divider()
   }
  }}}
 }
 func sourceLayouts(_ job:AudioJob)->[String]{
  guard let channels=job.info["channels"] as? Int else{return []}
  let standard=UpmixLayoutCatalogue.sourceLayouts.filter{$0.channels==channels}.map(\.id)
  let simple=channels==1 ? ["mono"]:channels==2 ? ["stereo"]:[]
  return simple+standard
 }
 func sourceLayoutName(_ id:String)->String{
  if id=="mono"{return model.text("單聲道")}
  if id=="stereo"{return model.text("雙聲道")}
  if let layout=UpmixLayoutCatalogue.sourceLayout(for:id){return model.text(layout.name)}
  return id
 }
 func receive(_ providers:[NSItemProvider],format:String?=nil)->Bool {
  guard !model.running,!model.choosingOutput,!flow.revealing,flow.filePanel == nil else{return false}
  if let i=CommandLine.arguments.firstIndex(of:"--ui-event-log"),i+1<CommandLine.arguments.count,let file=FileHandle(forWritingAtPath:CommandLine.arguments[i+1]){_ = try? file.seekToEnd();try? file.write(contentsOf:Data("{\"event\":\"swiftui-drop\",\"provider_count\":\(providers.count)}\n".utf8));try? file.close()}
  Task{@MainActor in var urls=[URL]();for provider in providers{let data:Data?=await withCheckedContinuation{c in provider.loadDataRepresentation(forTypeIdentifier:UTType.fileURL.identifier){d,_ in c.resume(returning:d)}};if let data,let url=URL(dataRepresentation:data,relativeTo:nil){urls.append(url)}};await flow.accept(urls,format:format)};return true
 }
}

@MainActor final class DropHostingView:NSHostingView<RadialView> {
 var dropFlow:WheelFlow?
 func acceptPasteboard(_ pasteboard:NSPasteboard)->Bool {
  guard let flow=dropFlow,!flow.model.running,!flow.model.choosingOutput,!flow.revealing,flow.filePanel==nil else{return false}
  let urls=droppedFileURLs(pasteboard);guard !urls.isEmpty else{return false}
  Task{await flow.accept(urls)};return true
 }
 override func draggingEntered(_ sender:NSDraggingInfo)->NSDragOperation{recordDropEvent("host-enter",sender,dropFlow);guard let flow=dropFlow,!flow.model.running,!flow.model.choosingOutput,!flow.revealing,flow.filePanel==nil else{return []};return droppedFileURLs(sender.draggingPasteboard).isEmpty ? []:.copy}
 override func performDragOperation(_ sender:NSDraggingInfo)->Bool{recordDropEvent("host-perform",sender,dropFlow);return acceptPasteboard(sender.draggingPasteboard)}
}
func wheelScreenPointer(_ event:NSEvent,window:NSWindow?)->NSPoint{
 // Queued events retain their own screen coordinates. Live cursor queries
 // can be ahead of the delivered mouse-down, especially during fast drags.
 if let point=event.cgEvent?.location{return NSPoint(x:point.x,y:-point.y)}
 return window?.convertPoint(toScreen:event.locationInWindow) ?? event.locationInWindow
}
struct ConvertSettingsView:View {
 @ObservedObject var model:ConverterModel
 @Environment(\.accessibilityReduceTransparency) private var reduceTransparency
 private var opaque:Bool{reduceTransparency || CommandLine.arguments.contains("--reduce-transparency-test")}
 var body:some View {
  VStack(spacing:22){
   HStack{
    Text(model.text("語言")).font(ConverterTypography.font(13,weight:.medium))
    Spacer()
    Picker(model.text("語言"),selection:$model.language){ForEach(AppLanguage.allCases,id:\.self){Text($0.name).tag($0)}}
     .labelsHidden().frame(width:160).accessibilityIdentifier("settings-language")
   }
   Divider()
   Group{
    if IrisArtwork.shared.available && !opaque{IrisRenderedBody(progress:0,count:4,pressed:false,shine:model.glassShine).frame(width:360,height:360).scaleEffect(0.58).frame(width:210,height:180)}
    else{BeveledGlassBadge(reduceTransparency:true).frame(width:130,height:130).frame(height:180)}
   }.allowsHitTesting(false).accessibilityHidden(true)
   VStack(spacing:10){
    HStack{Text(model.text("玻璃光澤")).font(ConverterTypography.font(13,weight:.semibold));Spacer();Text("\(Int((model.glassShine*100).rounded()))%").monospacedDigit().foregroundStyle(.secondary)}
    Slider(value:$model.glassShine,in:0...1).accessibilityLabel(model.text("玻璃光澤")).accessibilityIdentifier("glass-shine-slider").disabled(opaque || !IrisArtwork.shared.adjustable)
    HStack{Text(model.text("柔和"));Spacer();Text(model.text("光亮"))}.font(.caption).foregroundStyle(.secondary)
    Text(model.text(opaque ? "降低透明度已啟用":!IrisArtwork.shared.adjustable ? "玻璃素材尚未就緒":"拖動滑桿，直接預覽本體光澤。")).font(.caption).foregroundStyle(.secondary)
   }
   Button(model.text("恢復建議值")){model.glassShine=ConverterModel.defaultGlassShine}.disabled(opaque || !IrisArtwork.shared.adjustable)
  }.padding(28).frame(width:400).preferredColorScheme(.light)
 }
}
class FloatingWheel:NSPanel,NSDraggingDestination {
 weak var dropFlow:WheelFlow?
 private var windowDrag:(origin:NSPoint,pointer:NSPoint,surface:WheelInteractionView?,action:String?,moved:Bool)?
 func pointerLocation(for event:NSEvent)->NSPoint{
  wheelScreenPointer(event,window:self)
 }
 func performBodyClick(_ action:String?,surface:WheelInteractionView){surface.performClick(action)}
 override func sendEvent(_ event:NSEvent){
  if event.type == .leftMouseDown,attachedSheet == nil,dropFlow?.filePanel == nil,dropFlow?.model.choosingOutput != true{
   // Every live body region uses the same gesture. A stationary release is
   // its click action; movement cancels that action and moves the panel.
   let hitPoint=contentView?.superview?.convert(event.locationInWindow,from:nil) ?? event.locationInWindow
   let surface=contentView?.hitTest(hitPoint) as? WheelInteractionView
   let action=surface.flatMap{$0.region($0.convert(event.locationInWindow,from:nil))}
   if action != nil{
    dropFlow?.pressing=["file","convert"].contains(action ?? "")
    windowDrag=(frame.origin,pointerLocation(for:event),surface,action,false)
    makeKeyAndOrderFront(nil)
    return
   }
  }
  if event.type == .leftMouseDragged,var drag=windowDrag{
   let point=pointerLocation(for:event),dx=point.x-drag.pointer.x,dy=point.y-drag.pointer.y
   if hypot(dx,dy)>=4{drag.moved=true}
   windowDrag=drag
   if drag.moved{dropFlow?.pressing=false;setFrameOrigin(NSPoint(x:drag.origin.x+dx,y:drag.origin.y+dy))}
   return
  }
  if event.type == .leftMouseUp,let drag=windowDrag{
   let point=pointerLocation(for:event)
   let moved=drag.moved || hypot(point.x-drag.pointer.x,point.y-drag.pointer.y)>=4
   windowDrag=nil;dropFlow?.pressing=false
   if moved{
    setFrameOrigin(NSPoint(x:drag.origin.x+point.x-drag.pointer.x,y:drag.origin.y+point.y-drag.pointer.y))
   }else if let surface=drag.surface,drag.action == surface.region(surface.convert(event.locationInWindow,from:nil)){
    performBodyClick(drag.action,surface:surface)
   }
   return
  }
  super.sendEvent(event)
 }
 override var canBecomeKey:Bool{true}
 override var canBecomeMain:Bool{true}
 func draggingEntered(_ sender:NSDraggingInfo)->NSDragOperation{
  recordDropEvent("window-enter",sender,dropFlow)
  guard let flow=dropFlow,!flow.model.running,!flow.model.choosingOutput,!flow.revealing,flow.filePanel == nil else{return []}
  return droppedFileURLs(sender.draggingPasteboard).isEmpty ? []:.copy
 }
 func draggingUpdated(_ sender:NSDraggingInfo)->NSDragOperation{draggingEntered(sender)}
 func prepareForDragOperation(_ sender:NSDraggingInfo)->Bool{draggingEntered(sender) == .copy}
 func performDragOperation(_ sender:NSDraggingInfo)->Bool{
  recordDropEvent("window-perform",sender,dropFlow)
  guard let flow=dropFlow,draggingEntered(sender) == .copy else{return false}
  let urls=droppedFileURLs(sender.draggingPasteboard);Task{await flow.accept(urls)};return true
 }
}
@MainActor final class WheelDelegate:NSObject,NSApplicationDelegate {
 var languageObserver:AnyCancellable?
 var backdrop:NSWindow?;var screenCaptureSucceeded=false
 var window:FloatingWheel!;var model:ConverterModel!;var flow:WheelFlow!;var host:DropHostingView!
 var settingsWindow:NSWindow?
 func applicationDidFinishLaunching(_ notification:Notification){
  if let url=Bundle.main.url(forResource:"ConvertIcon",withExtension:"icns"),let icon=NSImage(contentsOf:url){
   NSApp.applicationIconImage=icon
  }
  model=ConverterModel();flow=WheelFlow(model)
  host=DropHostingView(rootView:RadialView(model:model,flow:flow));host.dropFlow=flow;host.registerForDraggedTypes([.fileURL,.URL,.init("NSFilenamesPboardType")]);host.frame=NSRect(origin:.zero,size:WheelPresentation.body.size)
  window=FloatingWheel(contentRect:host.frame,styleMask:[.borderless],backing:.buffered,defer:false)
  window.isReleasedWhenClosed=false;window.hidesOnDeactivate=false;window.isFloatingPanel=true;window.dropFlow=flow;window.registerForDraggedTypes([.fileURL,.URL,.init("NSFilenamesPboardType")]);flow.presentationWindow=window
  window.title="UTUVO convert";window.contentView=host;window.isOpaque=false;window.backgroundColor = .clear;window.hasShadow=false;window.level = .floating;window.isMovable=true;window.isMovableByWindowBackground=false
  window.acceptsMouseMovedEvents=true
  window.center();window.makeKeyAndOrderFront(nil);NSApp.activate(ignoringOtherApps:true)
  languageObserver=model.$language.sink{[weak self] language in self?.buildMenu(language:language);self?.settingsWindow?.title=language.text("設定")}
  if CommandLine.arguments.contains("--radial-demo"){Task{await demo()}}
  if CommandLine.arguments.contains("--hit-test"){Task{await hitTestDemo()}}
  if CommandLine.arguments.contains("--glass-demo"){Task{await glassDemo()}}
  if CommandLine.arguments.contains("--disc-review"){Task{await discReview()}}
  if CommandLine.arguments.contains("--cancel-after"){Task{await cancelAfter()}}
  if CommandLine.arguments.contains("--pasteboard-probe"){Task{await pasteboardProbe()}}
  if CommandLine.arguments.contains("--iris-demo"){Task{await irisDemo()}}
 }
 func buildMenu(language:AppLanguage){
  func t(_ key:String)->String{language.text(key)}
  let menu=NSMenu(),appItem=NSMenuItem();menu.addItem(appItem)
  let appMenu=NSMenu();let settingsItem=appMenu.addItem(withTitle:t("設定…"),action:#selector(showSettings),keyEquivalent:",");settingsItem.target=self
  appMenu.addItem(.separator());appMenu.addItem(withTitle:t("退出 UTUVO convert"),action:#selector(NSApplication.terminate(_:)),keyEquivalent:"q");appItem.submenu=appMenu
  let fileItem=NSMenuItem(title:t("檔案"),action:nil,keyEquivalent:""),files=NSMenu(title:t("檔案"));menu.addItem(fileItem)
  files.addItem(withTitle:t("加入音檔…"),action:#selector(openAudio),keyEquivalent:"o");files.addItem(withTitle:t("顯示 Convert"),action:#selector(showConvert),keyEquivalent:"");fileItem.submenu=files
  let editItem=NSMenuItem(title:t("編輯"),action:nil,keyEquivalent:""),edit=NSMenu(title:t("編輯"));menu.addItem(editItem)
  for (title,action,key) in [("剪下",#selector(NSText.cut(_:)),"x"),("複製",#selector(NSText.copy(_:)),"c"),("貼上",#selector(NSText.paste(_:)),"v"),("全選",#selector(NSText.selectAll(_:)),"a")]{edit.addItem(withTitle:t(title),action:action,keyEquivalent:key)};editItem.submenu=edit
  NSApp.mainMenu=menu
 }
 @objc func showSettings(){
  guard !model.choosingOutput,flow.filePanel == nil else{return}
  if settingsWindow == nil{
   let settings=NSWindow(contentRect:NSRect(x:0,y:0,width:400,height:470),styleMask:[.titled,.closable],backing:.buffered,defer:false)
   settings.isReleasedWhenClosed=false;settings.title=model.text("設定");settings.level = .floating
   settings.contentView=NSHostingView(rootView:ConvertSettingsView(model:model));settings.center();settingsWindow=settings
  }
  settingsWindow?.makeKeyAndOrderFront(nil);NSApp.activate(ignoringOtherApps:true)
 }
 @objc func openAudio(){flow.chooseFiles()}
 @objc func showConvert(){window.makeKeyAndOrderFront(nil);NSApp.activate(ignoringOtherApps:true)}
 func applicationShouldHandleReopen(_ sender:NSApplication,hasVisibleWindows flag:Bool)->Bool{showConvert();return true}
 func application(_ application:NSApplication,open urls:[URL]){showConvert();guard !model.running,!flow.revealing else{flow.message="正在處理音檔，請完成後再加入。";return};Task{await flow.accept(urls)}}
 func applicationShouldTerminate(_ sender:NSApplication)->NSApplication.TerminateReply{
  guard model?.running == true else{return .terminateNow}
  let alert=NSAlert();alert.messageText=model.text("仍在處理音檔");alert.informativeText=model.text("取消處理並離開？已完成的輸出會保留。");alert.addButton(withTitle:model.text("繼續處理"));alert.addButton(withTitle:model.text("取消並離開"))
  guard alert.runModal() == .alertSecondButtonReturn else{return .terminateCancel}
  model.cancel();Task{while model.running{try? await Task.sleep(nanoseconds:50_000_000)};sender.reply(toApplicationShouldTerminate:true)};return .terminateLater
 }
 func irisDemo() async {
  func pause(_ ms:UInt64=80) async{try? await Task.sleep(nanoseconds:ms*1_000_000)}
  let reduced=CommandLine.arguments.contains("--reduce-motion-test")
  let opaque=CommandLine.arguments.contains("--reduce-transparency-test")
  let prefix="iris-"+(reduced ? "reduced":opaque ? "opaque":"full")
  var frames=[[String:Any]]()
  makeGlassBackdrop();await pause(200)
  await captureGlass(prefix+"-idle")
  let task=Task{@MainActor in await flow.accept([model.root.appendingPathComponent("fixtures/moving-adm.wav")])}
  for _ in 0..<80{if flow.phase == .format{break};await pause(20)}
  let started=Date()
  for i in 0..<22{
   let name=prefix+"-open-"+String(format:"%02d",i)
   await captureGlass(name)
   frames.append(["file":"radial-"+name+".png","elapsed":Date().timeIntervalSince(started),"stage":"open"])
   await pause(45)
  }
  await task.value;await pause(120)
  await captureGlass(prefix+"-open-final")
  for kind in ["light","busy"]{
   backdrop?.close();backdrop=nil;makeGlassBackdrop(light:kind == "light",busy:kind == "busy");await pause(120)
   await captureGlass(prefix+"-"+kind)
  }
  backdrop?.close();backdrop=nil;makeGlassBackdrop();await pause(80)
  flow.reset();let closing=Date()
  for i in 0..<16{
   let name=prefix+"-close-"+String(format:"%02d",i)
   await captureGlass(name)
   frames.append(["file":"radial-"+name+".png","elapsed":Date().timeIntervalSince(closing),"stage":"close"])
   await pause(45)
  }
  await pause(150);await captureGlass(prefix+"-closed-final")
  // Reverse a live opening without resetting its displayed playhead.
  let first=Task{@MainActor in await flow.accept([model.root.appendingPathComponent("fixtures/moving-adm.wav")])}
  for _ in 0..<80{if flow.phase == .format{break};await pause(20)}
  await pause(440);await captureGlass(prefix+"-interrupt-before")
  flow.reset();await captureGlass(prefix+"-interrupt-after");await pause(1100);await first.value
  await captureGlass(prefix+"-interrupt-closed")
  await flow.accept([model.root.appendingPathComponent("fixtures/moving-stereo.wav")])
  flow.choose("WAV");await pause(150);await captureGlass(prefix+"-five-layouts")
  flow.reset();await pause(1100)
  await flow.accept([model.root.appendingPathComponent("fixtures/moving-adm.wav")])
  flow.choose("WAV");flow.choose("stereo")
  model.outDir=model.root.appendingPathComponent(".build/iris-qc-"+UUID().uuidString).path
  flow.begin();while model.running{await pause(100)};await pause(200)
  let outputs=model.jobs.compactMap(\.output)
  let result:[String:Any]=["frames":frames,"outputs":outputs,"valid_jobs":flow.audioJobs.count,
   "artwork_available":IrisArtwork.shared.available,"authored_frames":IrisArtwork.shared.frames.count,
   "used_screen_capturekit":screenCaptureSucceeded,"reduced_motion":reduced,"reduce_transparency":opaque,
   "scope":"Canonical App, synthetic fixtures and own synthetic background only. Actual on-screen render; native flow calls, not physical Finder acceptance."]
  try? JSONSerialization.data(withJSONObject:result,options:.prettyPrinted).write(to:model.root.appendingPathComponent("evidence/"+prefix+".json"))
  NSApp.terminate(nil)
 }
 func pasteboardProbe() async {
  var rows=[[String:Any]]()
  var inputs=[model.root.appendingPathComponent("fixtures/moving-adm.wav")]
  if let i=CommandLine.arguments.firstIndex(of:"--ui-fixture-directory"),i+1<CommandLine.arguments.count{inputs.append(URL(fileURLWithPath:CommandLine.arguments[i+1]).appendingPathComponent("synthetic-adm.wav"))}
  for url in inputs{
   let p=NSPasteboard(name:.init("local.utuvo.probe."+UUID().uuidString));p.clearContents();let written=p.writeObjects([url as NSURL])
   let raw=p.readObjects(forClasses:[NSURL.self],options:[.urlReadingFileURLsOnly:true])
   let state:[String:Any]=["fixture":url.lastPathComponent,"file_exists":FileManager.default.fileExists(atPath:url.path),"written":written,"types":p.types?.map(\.rawValue) ?? [],"raw_count":raw?.count ?? -1,"decoded_count":droppedFileURLs(p).count,"host_has_flow":host.dropFlow != nil,"running_before":model.running,"revealing_before":flow.revealing]
   let accepted=host.acceptPasteboard(p)
   try? await Task.sleep(nanoseconds:150_000_000);while model.running || flow.revealing{try? await Task.sleep(nanoseconds:100_000_000)}
   rows.append(state.merging(["accepted":accepted,"jobs":model.jobs.count,"valid_jobs":flow.audioJobs.count]){_,new in new});flow.reset()
  }
  try? JSONSerialization.data(withJSONObject:rows,options:.prettyPrinted).write(to:model.root.appendingPathComponent("evidence/pasteboard-probe.json"));print(rows);NSApp.terminate(nil)
 }
 func applicationShouldTerminateAfterLastWindowClosed(_ sender:NSApplication)->Bool{
  // NSPanel is not counted like a document window when NSOpenPanel closes.
  return window?.isVisible != true
 }
 func cancelAfter() async {
  func pause() async{try? await Task.sleep(nanoseconds:100_000_000)}
  await flow.accept([model.root.appendingPathComponent("fixtures/moving-adm.wav")])
  let before=model.jobs.map(\.id),phase=flow.phase
  var records=[[String:Any]]()
  flow.panelObserver={panel in
   panel.directoryURL=self.model.root.appendingPathComponent("fixtures")
   let timer=Timer(timeInterval:0.25,repeats:false){[weak panel] _ in MainActor.assumeIsolated{panel?.cancel(nil)}}
   RunLoop.main.add(timer,forMode:.common)
  }
  for i in 1...5{
   flow.chooseFiles();let opened=flow.filePanel != nil
   while flow.filePanel != nil{await pause()};await pause()
   records.append(["cycle":i,"actual_sheet_opened":opened,"queue_unchanged":model.jobs.map(\.id)==before,"phase_unchanged":flow.phase==phase,"main_visible":window.isVisible])
  }
  NSApp.deactivate();await pause();let remainsVisible=window.isVisible
  NSApp.activate(ignoringOtherApps:true)
  flow.panelObserver={panel in
   panel.directoryURL=self.model.root.appendingPathComponent("fixtures")
   panel.nameFieldStringValue="moving-stereo.wav"
   let timer=Timer(timeInterval:0.8,repeats:false){[weak panel] _ in MainActor.assumeIsolated{panel?.ok(nil)}}
   let fallback=Timer(timeInterval:3,repeats:false){[weak panel] _ in MainActor.assumeIsolated{if panel?.isVisible == true{panel?.cancel(nil)}}}
   RunLoop.main.add(timer,forMode:.common);RunLoop.main.add(fallback,forMode:.common)
  }
  flow.chooseFiles();while flow.filePanel != nil{await pause()};await pause();while model.running || flow.revealing{await pause()}
  let selectedAfter=model.jobs.contains{$0.name == "moving-stereo.wav"}
  flow.panelObserver=nil;await captureGlass("interaction-cancelled")
  func key(_ chars:String,_ code:UInt16){for kind in [NSEvent.EventType.keyDown,.keyUp]{if let e=NSEvent.keyEvent(with:kind,location:.zero,modifierFlags:[],timestamp:ProcessInfo.processInfo.systemUptime,windowNumber:window.windowNumber,context:nil,characters:chars,charactersIgnoringModifiers:chars,isARepeat:false,keyCode:code){window.sendEvent(e)}}}
  window.makeKeyAndOrderFront(nil)
  key("1",18);await pause();key("\r",36);await pause();key("1",18);await pause()
  model.outDir=model.root.appendingPathComponent(".build/cancel-after-"+String(Int(Date().timeIntervalSince1970))).path
  key("\r",36);await pause();while model.running{await pause()}
  let outputs=model.jobs.compactMap(\.output)

  let record:[String:Any]=["cancel_cycles":records,"scope":"Real NSOpenPanel sheets and native Cancel action; not physical keyboard Escape or Finder drag","hides_on_deactivate":window.hidesOnDeactivate,"visible_after_deactivation":remainsVisible,"select_after_cancel_via_actual_panel":selectedAfter,"outputs_after_cancel":outputs,"conversion_after_cancel":!outputs.isEmpty]
  try! JSONSerialization.data(withJSONObject:record,options:.prettyPrinted).write(to:model.root.appendingPathComponent("evidence/cancel-after.json"))
  NSApp.terminate(nil)
 }
 func capture(_ name:String){
  host.layoutSubtreeIfNeeded();guard let rep=host.bitmapImageRepForCachingDisplay(in:host.bounds) else{return};host.cacheDisplay(in:host.bounds,to:rep)
  if let data=rep.representation(using:.png,properties:[:]){try? data.write(to:model.root.appendingPathComponent("evidence/radial-"+name+".png"))}
 }
 func captureGlass(_ name:String,close:Bool=false) async {
  // Never request permission. Filter to this process's exact window only.
  guard CGPreflightScreenCaptureAccess() else{capture(name);return}
  do{
   let shareable=try await SCShareableContent.excludingDesktopWindows(true,onScreenWindowsOnly:true)
   guard let own=shareable.windows.first(where:{$0.windowID == CGWindowID(window.windowNumber) && $0.owningApplication?.processID == ProcessInfo.processInfo.processIdentifier}) else{capture(name);return}
   let config=SCStreamConfiguration();config.width=close ? 360:Int(host.bounds.width*2);config.height=close ? 360:Int(host.bounds.height*2);config.showsCursor=false;config.ignoreShadowsSingleWindow=true
   var filter=SCContentFilter(desktopIndependentWindow:own)
   if close{config.sourceRect=CGRect(x:130,y:146,width:180,height:180)}
   if let backdrop,let synthetic=shareable.windows.first(where:{$0.windowID == CGWindowID(backdrop.windowNumber) && $0.owningApplication?.processID == ProcessInfo.processInfo.processIdentifier}),let display=shareable.displays.first(where:{$0.frame.intersects(own.frame)}) {
    // Include exactly these two app-owned windows, excluding desktop, dock and other apps.
    filter=SCContentFilter(display:display,including:[own,synthetic])
    config.sourceRect=CGRect(x:own.frame.minX-display.frame.minX+(close ? 130:0),y:own.frame.minY-display.frame.minY+(close ? 146:0),width:close ? 180:own.frame.width,height:close ? 180:own.frame.height)
   }
   let cg=try await SCScreenshotManager.captureImage(contentFilter:filter,configuration:config)
   let rep=NSBitmapImageRep(cgImage:cg)
   if let png=rep.representation(using:.png,properties:[:]){try png.write(to:model.root.appendingPathComponent("evidence/radial-"+name+".png"));screenCaptureSucceeded=true}
  }catch{capture(name)}
 }
 func makeGlassBackdrop(light:Bool=false,busy:Bool=false){
  let frame=window.frame.insetBy(dx:-30,dy:-30)
  let panel=NSWindow(contentRect:frame,styleMask:[.borderless],backing:.buffered,defer:false)
  panel.isReleasedWhenClosed=false;panel.title="Synthetic glass backdrop";panel.level=NSWindow.Level(rawValue:NSWindow.Level.floating.rawValue-1)
  let colors=busy ? [Color(red:0.92,green:0.42,blue:0.18),Color(red:0.22,green:0.43,blue:0.87),Color(red:0.75,green:0.18,blue:0.57)]:light ? [.white,.white,.white]:[.black,.black,.black]
  panel.contentView=NSHostingView(rootView:LinearGradient(colors:colors,startPoint:.topLeading,endPoint:.bottomTrailing).overlay {
   HStack(spacing:busy ? 22:45){ForEach(0..<12){i in Rectangle().fill((i%2==0 ? Color.white:Color.black).opacity(busy ? 0.35:0)).frame(width:busy ? 18:2)}}.rotationEffect(.degrees(-28))
  });panel.orderFront(nil);window.orderFront(nil);backdrop=panel
 }
 func discReview() async {
  var samples=[[String:Any]]()
  for kind in ["dark","light","busy"]{
   backdrop?.close();backdrop=nil;makeGlassBackdrop(light:kind == "light",busy:kind == "busy")
   try? await Task.sleep(nanoseconds:350_000_000)
   let name="disc-"+kind
   await captureGlass(name)
   await captureGlass(name+"-close",close:true)
   samples.append(["background":kind,"screen_capture":screenCaptureSucceeded,"phase":flow.phase.rawValue,"jobs":model.jobs.count])
  }
  try? JSONSerialization.data(withJSONObject:samples,options:.prettyPrinted).write(to:model.root.appendingPathComponent("evidence/disc-review.json"))
  NSApp.terminate(nil)
 }
 func glassDemo() async {
  func pause(_ ms:UInt64=80) async{try? await Task.sleep(nanoseconds:ms*1_000_000)}
  func drop(_ path:URL)->Bool{let p=NSPasteboard(name:.init("local.utuvo.glass-fixture"));p.clearContents();p.writeObjects([path as NSURL]);return host.acceptPasteboard(p)}
  func key(_ chars:String,_ code:UInt16){for kind in [NSEvent.EventType.keyDown,.keyUp]{if let e=NSEvent.keyEvent(with:kind,location:.zero,modifierFlags:[],timestamp:ProcessInfo.processInfo.systemUptime,windowNumber:window.windowNumber,context:nil,characters:chars,charactersIgnoringModifiers:chars,isARepeat:false,keyCode:code){window.sendEvent(e)}}}
  let suffix=CommandLine.arguments.contains("--reduce-motion-test") ? "reduced":"full"
  makeGlassBackdrop();await pause(250);await captureGlass("glass-"+suffix+"-00-idle")
  let beforeReveal=flow.revealID
  let accepted=drop(model.root.appendingPathComponent("fixtures/moving-adm.wav"))
  for _ in 0..<150{if flow.revealID>beforeReveal{break};await pause(40)}
  for i in 1...20{await captureGlass("glass-"+suffix+"-"+String(format:"%02d",i));await pause(70)}
  while model.running || flow.revealing{await pause()}
  let firstReveal=flow.revealID
  _=drop(model.root.appendingPathComponent("fixtures/moving-stereo.wav"));await pause(200);while model.running || flow.revealing{await pause()}
  let appendOK=model.jobs.count==2 && flow.revealID==firstReveal
  await captureGlass("glass-"+suffix+"-21-append")
  flow.reset();await pause(200);await captureGlass("glass-"+suffix+"-22-reset")
  let invalid=model.root.appendingPathComponent(".build/invalid-glass.wav");try? Data("invalid synthetic fixture".utf8).write(to:invalid)
  _=drop(invalid);await pause(200);while model.running{await pause()};await pause(100)
  let invalidOK=flow.phase == .idle && flow.audioJobs.isEmpty && !flow.message.isEmpty
  await captureGlass("glass-"+suffix+"-23-invalid")
  flow.reset();_=drop(model.root.appendingPathComponent("fixtures/moving-stereo.wav"));await pause(200);while model.running || flow.revealing{await pause()};await pause(100)
  key("1",18);await pause(180);key("\r",36);await pause(180);key("2",19);await pause(180)
  model.outDir=model.root.appendingPathComponent(".build/glass-ordinary-"+suffix+"-"+String(Int(Date().timeIntervalSince1970))).path
  key("\r",36);await pause(200);while model.running{await pause()};await pause(200)
  let data=try! JSONSerialization.data(withJSONObject:["native_pasteboard_ingress":accepted,"append_without_replay":appendOK,"invalid_stays_idle":invalidOK,"ordinary_keyboard_export":model.jobs.allSatisfy{$0.output != nil},"outputs":model.jobs.compactMap(\.output),"reduce_motion_test_override":suffix == "reduced","capture_used_screen_capturekit":screenCaptureSucceeded,"synthetic_backdrop_only":true,"native_glass_available":ProcessInfo.processInfo.isOperatingSystemAtLeast(.init(majorVersion:26,minorVersion:0,patchVersion:0)),"scope":"Native pasteboard ingestion and actual NSWindow events; animation frames captured from own app window (ScreenCaptureKit when already authorized, hosting-view fallback). Synthetic backdrop only. Not physical Finder drag."],options:.prettyPrinted)
  try! data.write(to:model.root.appendingPathComponent("evidence/glass-"+suffix+"-acceptance.json"));NSApp.terminate(nil)
 }
 func hitTestDemo() async {
  func pause() async {try? await Task.sleep(nanoseconds:300_000_000)}
  func click(_ x:CGFloat,_ y:CGFloat) {
   let point=host.convert(NSPoint(x:x,y:y),to:nil)
   for kind in [NSEvent.EventType.leftMouseDown,.leftMouseUp] {
    if let e=NSEvent.mouseEvent(with:kind,location:point,modifierFlags:[],timestamp:ProcessInfo.processInfo.systemUptime,windowNumber:window.windowNumber,context:nil,eventNumber:1,clickCount:1,pressure:1){window.sendEvent(e)}
   }
  }
  func key(_ chars:String,_ code:UInt16){for kind in [NSEvent.EventType.keyDown,.keyUp]{if let e=NSEvent.keyEvent(with:kind,location:.zero,modifierFlags:[],timestamp:ProcessInfo.processInfo.systemUptime,windowNumber:window.windowNumber,context:nil,characters:chars,charactersIgnoringModifiers:chars,isARepeat:false,keyCode:code){window.sendEvent(e)}}}
  try? await Task.sleep(nanoseconds:250_000_000)
  let board=NSPasteboard(name:NSPasteboard.Name("local.utuvo.synthetic-drop."+UUID().uuidString));board.clearContents();let written=board.writeObjects([model.root.appendingPathComponent("fixtures/moving-adm.wav") as NSURL])
  let accepted=host.acceptPasteboard(board)
  await pause();while model.running || flow.revealing{await pause()};await pause()
  guard written,accepted,flow.audioJobs.count==1 else{
   let failure:[String:Any]=["status":"FAILED before pointer dispatch","written":written,"accepted":accepted,"url_count":droppedFileURLs(board).count,"job_count":model.jobs.count,"valid_job_count":flow.audioJobs.count,"types":board.types?.map(\.rawValue) ?? [],"scope":"Native test ingestion; no pointer/export success claimed"]
   try? JSONSerialization.data(withJSONObject:failure,options:.prettyPrinted).write(to:model.root.appendingPathComponent("evidence/pointer-after.json"));NSApp.terminate(nil);return
  }
  var records=[[String:Any]]()
  for (name,x,y) in [("WAV",210.0,91.0),("AIFF",331.0,212.0),("FLAC",210.0,333.0),("OGG",89.0,212.0),("WAV",210.0,60.0),("AIFF",365.0,212.0),("FLAC",210.0,363.0),("OGG",55.0,212.0)] {
   click(x,y);await pause()
   records.append(["expected":name,"format":model.format,"selected":flow.selected ?? "","phase":flow.phase.rawValue,"hit_view":host.hitTest(NSPoint(x:x,y:y)).map{String(describing:type(of:$0))} ?? "none","hosting_view_flipped":host.isFlipped,"window_key":window.isKeyWindow,"pass":model.format==name && flow.selected==name && flow.phase == .format])
  }
  // Disabled OGG 5.1 must explain the restriction instead of silently failing.
  click(210,251);await pause()
  let mouseNext=flow.phase == .layout
  click(331,212);await pause()
  let unsupportedExplained=flow.phase == .layout && !flow.message.isEmpty
  capture("fix-00-unavailable")
  flow.back();await pause()
  key("1",18);await pause()
  let keyboardFormat=flow.selected == "WAV" && model.format == "WAV"
  click(210,91);await pause();capture("fix-01-format")
  // Next is a visible SwiftUI button: use Return through AppKit key dispatch.
  key("\r",36);await pause();capture("fix-02-layout")
  let nextWorked=flow.phase == .layout
  // ADM has four layout petals: stereo at the top.
  click(210,91);await pause();capture("fix-03-convert")
  let reviewWorked=flow.phase == .review && model.target=="stereo"
  model.outDir=model.root.appendingPathComponent(".build/pointer-fix-"+String(Int(Date().timeIntervalSince1970))).path
  click(210,212);await pause();capture("fix-04-progress")
  let mouseStarted=model.running || model.jobs.contains{$0.output != nil}
  while model.running || flow.revealing{await pause()};await pause();capture("fix-05-result")
  let firstOutputs=model.jobs.compactMap(\.output),firstStatus=model.jobs.map(\.status)
  var exports=[[String:Any]]()
  for (name,x,y) in [("AIFF",331.0,212.0),("FLAC",210.0,333.0),("OGG",89.0,212.0)] {
   flow.reset();_=host.acceptPasteboard(board);await pause();while model.running || flow.revealing{await pause()};await pause()
   click(x,y);await pause();click(210,251);await pause();click(210,91);await pause()
   model.outDir=model.root.appendingPathComponent(".build/pointer-fix-"+name+"-"+String(Int(Date().timeIntervalSince1970))).path
   click(210,212);await pause();while model.running || flow.revealing{await pause()};await pause()
   exports.append(["format":name,"outputs":model.jobs.compactMap(\.output),"status":model.jobs.map(\.status),"pass":!model.jobs.isEmpty && model.jobs.allSatisfy{$0.output != nil}])
  }
  flow.reset();_=host.acceptPasteboard(board);await pause();while model.running || flow.revealing{await pause()};await pause()
  key("1",18);await pause();key("\r",36);await pause();key("1",18);await pause()
  model.outDir=model.root.appendingPathComponent(".build/pointer-fix-keyboard-"+String(Int(Date().timeIntervalSince1970))).path
  key("\r",36);await pause();while model.running || flow.revealing{await pause()};await pause()
  let keyboardExport = !model.jobs.isEmpty && model.jobs.allSatisfy{$0.output != nil}
  let keyboardOutputs=model.jobs.compactMap(\.output)
  let evidence:[String:Any]=["native_pasteboard_drop_ingress":accepted,"synthetic_fixture_only":true,"pointer_events":records,"mouse_next":mouseNext,"keyboard_format":keyboardFormat,"unsupported_explained":unsupportedExplained,"return_next":nextWorked,"mouse_layout":reviewWorked,"mouse_convert_started":mouseStarted,"keyboard_full_export":keyboardExport,"keyboard_outputs":keyboardOutputs,"exports":exports,"outputs":firstOutputs,"status":firstStatus,"scope":"AppKit dispatch through real window hit testing; not a physical Finder drag or global input injection"]
  let data=try! JSONSerialization.data(withJSONObject:evidence,options:.prettyPrinted)
  try! data.write(to:model.root.appendingPathComponent("evidence/pointer-after.json"));NSApp.terminate(nil)
 }
 func demo() async {
  func pause() async {try? await Task.sleep(nanoseconds:450_000_000)}
  await pause();capture("01-drop")
  await flow.accept([model.root.appendingPathComponent("fixtures/moving-adm.wav")]);await pause();flow.hover="WAV";await pause();capture("02-format-hover")
  flow.choose("WAV");await pause();flow.hover="stereo";await pause();capture("03-layout-hover")
  flow.choose("stereo");await pause();flow.cancel();await pause();capture("04-cancel")
  flow.choose("WAV");flow.choose("stereo")
  model.outDir=model.root.appendingPathComponent(".build/radial-demo-"+String(Int(Date().timeIntervalSince1970))).path
  await pause();capture("05-review");flow.begin();await pause();capture("06-progress")
  while model.running{try? await Task.sleep(nanoseconds:100_000_000)}
  flow.phase = .result;await pause();capture("07-result")
  let outputs=model.jobs.compactMap(\.output)
  let evidence:[String:Any]=["scripted_native_interaction":true,"actions":["synthetic file accepted using drop action","WAV hover","WAV select","Stereo hover","cancel selection","WAV select","Stereo select","convert via real worker"],"outputs":outputs,"status":model.jobs.map(\.status),"notice":model.notice]
  if let data=try? JSONSerialization.data(withJSONObject:evidence,options:.prettyPrinted){try? data.write(to:model.root.appendingPathComponent("evidence/radial-demo.json"))}
 }
}
@main @MainActor struct RadialApplication {static func main(){let app=NSApplication.shared;app.setActivationPolicy(.regular);let delegate=WheelDelegate();app.delegate=delegate;withExtendedLifetime(delegate){app.run()}}}
