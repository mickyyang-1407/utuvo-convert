import SwiftUI
import AppKit
import UniformTypeIdentifiers
struct AudioJob: Identifiable {
 let id=UUID(); var path:String; var name:String; var info:[String:Any]=[:]
 var status="待分析"; var detail=""; var layout="unknown"; var progress=0.0; var output:String?; var programme=""
}

/// A single implemented layout entry loaded from the bundled catalogue.
struct UpmixLayoutChoice:Identifiable,Equatable {
 let id:String          // catalogue id, e.g. "5.1-side"
 let name:String        // canonical human name, e.g. "5.1 Side"
 let channels:Int
 let hasHeight:Bool
 let hasCentre:Bool
}

/// One catalogue reference row (not selectable, surfaced for documentation).
struct UpmixReferenceEntry:Identifiable,Equatable {
 let id:String          // catalogue id
 let name:String        // human label, falls back to id
 let kind:String
 let channels:Int
}

/// Catalogue loader. The same JSON the Python backend uses is bundled
/// next to the engine and read once into memory. No equality-by-channel-count
/// inference is ever performed anywhere in the App: every layout label,
/// channel count and human name comes from this registry.
@MainActor enum UpmixLayoutCatalogue {
 static let v1TrialLayoutId = "5.1-side"
 static let v2DefaultLayoutId = "5.1-side"
 static let v2WheelLeafLayoutId = "7.1.4-rear-first"
 private struct AmountRange { let min:Double; let max:Double; let value:Double }
 private struct Catalogue {
  let version:String
  let implemented:[UpmixLayoutChoice]
  let inputOrders:[UpmixLayoutChoice]
  let reference:[UpmixReferenceEntry]
  let centre:AmountRange
  let surround:AmountRange
  let height:AmountRange
  let supportedRates:[Int]
 }
 private enum CatalogueError:Error { case invalid(String) }
 private static var cached:Catalogue?
 private static var cachedRoot:String?
 private(set) static var loadError:String?

 /// Load the canonical Python catalogue from the selected engine root. The
 /// App model calls this before reading any catalogue-backed values, and
 /// tests can point at an isolated engine tree.
 static func configure(engineRoot:URL) {
  let root=engineRoot.resolvingSymlinksInPath().standardizedFileURL
  guard cachedRoot != root.path else{return}
  cachedRoot=root.path;cached=nil;loadError=nil
  do { cached=try readCatalogue(root:root) }
  catch { loadError=error.localizedDescription }
 }
 private static func readCatalogue(root:URL)throws->Catalogue {
  let url=root.appendingPathComponent("backend/upmix_layouts.json")
  guard let object=try JSONSerialization.jsonObject(with:Data(contentsOf:url)) as? [String:Any],
        let version=object["version"] as? String,version=="upmix-layouts-v2",
        let rawImplemented=object["implemented"] as? [[String:Any]],rawImplemented.count==10,
        let rawInput=object["explicit_input_orders"] as? [String:[String:Any]],rawInput.count==1,
        let rawReference=object["reference"] as? [[String:Any]],rawReference.count==18 else {
   throw CatalogueError.invalid("Invalid Upmix layout catalogue header or row count.")
  }
  let expected=Set(["quad","5.0-side","5.1-side","5.1-rear","7.0","7.1","5.1.2","5.1.4","7.1.2","7.1.4-rear-first"])
  var seen=Set<String>()
  let implemented=try rawImplemented.map { row -> UpmixLayoutChoice in
   guard let id=row["id"] as? String,!id.isEmpty,seen.insert(id).inserted,
         let name=row["name"] as? String,!name.isEmpty,
         let labels=row["labels"] as? [String],let channels=row["channels"] as? Int,
         channels>0,labels.count==channels,
         let hasHeight=row["has_height"] as? Bool,
         let hasCentre=row["has_centre"] as? Bool else {
    throw CatalogueError.invalid("Invalid or duplicate implemented Upmix layout row.")
   }
   return UpmixLayoutChoice(id:id,name:name,channels:channels,hasHeight:hasHeight,hasCentre:hasCentre)
  }
  guard seen==expected else{throw CatalogueError.invalid("Implemented Upmix layout ids do not match the supported set.")}
  let inputOrders=try rawInput.map { key,row -> UpmixLayoutChoice in
   guard let id=row["id"] as? String,id==key,id=="7.1.4-side-first",
         let name=row["name"] as? String,!name.isEmpty,
         let labels=row["labels"] as? [String],let channels=row["channels"] as? Int,
         channels==12,labels.count==channels,row["input_only"] as? Bool == true,
         let hasHeight=row["has_height"] as? Bool,
         let hasCentre=row["has_centre"] as? Bool else{
    throw CatalogueError.invalid("Invalid explicit Upmix input-order row.")
   }
   return UpmixLayoutChoice(id:id,name:name,channels:channels,hasHeight:hasHeight,hasCentre:hasCentre)
  }
  seen=[]
  let reference=try rawReference.map { row -> UpmixReferenceEntry in
   guard let id=row["id"] as? String,!id.isEmpty,seen.insert(id).inserted,
         let channels=row["channels"] as? Int,channels>0,
         let kind=row["kind"] as? String else{
    throw CatalogueError.invalid("Invalid or duplicate reference Upmix layout row.")
   }
   return UpmixReferenceEntry(id:id,name:id,kind:kind,channels:channels)
  }
  func amount(_ rangeKey:String,_ defaultKey:String)throws->AmountRange {
   guard let values=object[rangeKey] as? [Double],values.count==2,
         let fallback=object[defaultKey] as? Double,
         values.allSatisfy(\.isFinite),fallback.isFinite,
         values[0]<=values[1],(values[0]...values[1]).contains(fallback) else{
    throw CatalogueError.invalid("Invalid or non-finite Upmix amount range: \(rangeKey)")
   }
   return AmountRange(min:values[0],max:values[1],value:fallback)
  }
  guard let rates=object["rates_supported"] as? [Int],!rates.isEmpty,
        rates.allSatisfy({$0>0}),Set(rates).count==rates.count else{
   throw CatalogueError.invalid("Invalid supported Upmix sample rates.")
  }
  guard Set(reference.map(\.id)).isDisjoint(with:expected),
        Set(reference.map(\.id)).isDisjoint(with:Set(inputOrders.map(\.id))) else{
   throw CatalogueError.invalid("Reference Upmix ids overlap selectable or input-only ids.")
  }
  return Catalogue(version:version,implemented:implemented,inputOrders:inputOrders,reference:reference,
                   centre:try amount("centre_range","centre_default"),
                   surround:try amount("surround_budget_range","surround_budget_default"),
                   height:try amount("height_range","height_default"),supportedRates:rates)
 }
 private static var value:Catalogue {
  if let cached{return cached}
  let root=Bundle.main.resourceURL?.appendingPathComponent("OfflineEngine")
   ?? Bundle.main.bundleURL.deletingLastPathComponent().deletingLastPathComponent()
  configure(engineRoot:root)
  guard let cached else{fatalError("Unable to load Upmix catalogue: \(loadError ?? "missing catalogue")")}
  return cached
 }
 static var version:String { value.version }
 static var implemented:[UpmixLayoutChoice] { value.implemented }
 static var inputOrders:[UpmixLayoutChoice] { value.inputOrders }
 static var sourceLayouts:[UpmixLayoutChoice] { value.implemented + value.inputOrders }
 static var reference:[UpmixReferenceEntry] { value.reference }
 static var amountRanges:(centre:(min:Double,max:Double,default:Double),surround:(min:Double,max:Double,default:Double),height:(min:Double,max:Double,default:Double)) {
  let c=value.centre,s=value.surround,h=value.height
  return (centre:(c.min,c.max,c.value),surround:(s.min,s.max,s.value),height:(h.min,h.max,h.value))
 }
 static var supportedRates:[Int] { value.supportedRates }

 static func layout(for id:String) -> UpmixLayoutChoice? {
  implemented.first { $0.id == id }
 }
 static func sourceLayout(for id:String) -> UpmixLayoutChoice? {
  sourceLayouts.first { $0.id == id }
 }

 static var v1TrialLayout:UpmixLayoutChoice { layout(for: v1TrialLayoutId)! }
 static var v2DefaultLayout:UpmixLayoutChoice { layout(for: v2DefaultLayoutId)! }
 static var v2WheelLeafLayout:UpmixLayoutChoice { layout(for: v2WheelLeafLayoutId)! }
}

@MainActor final class ConverterModel: ObservableObject {
 @Published var jobs=[AudioJob](); @Published var format="WAV"; @Published var depth="24"
 @Published var target="stereo"; @Published var rate="0"; @Published var outDir=""
 @Published var mp3Bitrate=320
 var lossyFormat:Bool{format == "OGG" || format == "MP3"}
 var availableRates:[String]{format == "MP3" ? ["44100","48000"]:["44100","48000","88200","96000","176400","192000"]}
 @Published var running=false; @Published var notice="拖入音檔或資料夾，加入批次清單。"; @Published var overall=0.0
 @Published var analysing=false; @Published var choosingOutput=false
 @Published var upmixCentre=0.0 {
  didSet {
   // Persist a sanitised value so a future load cannot resurrect an
   // out-of-range amount. Live edits are kept as-is so the issue() gate
   // continues to see the raw value and report a refusal.
   if AppLanguage.commandLineOverride == nil {
    let ranges = UpmixLayoutCatalogue.amountRanges.centre
    let safe = ConverterModel.sanitizeAmount(upmixCentre, default: ranges.default, lo: ranges.min, hi: ranges.max)
    preferences.set(safe, forKey: ConverterModel.upmixCentreKey)
   }
  }
 }
 @Published var upmixSurround=0.4 {
  didSet {
   if AppLanguage.commandLineOverride == nil {
    let ranges = UpmixLayoutCatalogue.amountRanges.surround
    let safe = ConverterModel.sanitizeAmount(upmixSurround, default: ranges.default, lo: ranges.min, hi: ranges.max)
    preferences.set(safe, forKey: ConverterModel.upmixSurroundKey)
   }
  }
 }
 /// Height amount for the v2 generic upmix path. Range 0..0.5, default 0.2.
 @Published var upmixHeight=0.2 {
  didSet {
   if AppLanguage.commandLineOverride == nil {
    let ranges = UpmixLayoutCatalogue.amountRanges.height
    let safe = ConverterModel.sanitizeAmount(upmixHeight, default: ranges.default, lo: ranges.min, hi: ranges.max)
    preferences.set(safe, forKey: ConverterModel.upmixHeightKey)
   }
  }
 }
 /// Selected upmix output layout id (one of the implemented catalogue ids).
 @Published var upmixLayout:String = UpmixLayoutCatalogue.v2DefaultLayoutId {
  didSet {
   let sanitized = ConverterModel.sanitizeUpmixLayout(upmixLayout)
   if sanitized != upmixLayout { upmixLayout = sanitized; return }
   if AppLanguage.commandLineOverride == nil {
    preferences.set(upmixLayout, forKey: ConverterModel.upmixLayoutKey)
   }
  }
 }
 @Published var language:AppLanguage = .traditionalChinese {
  didSet{if AppLanguage.commandLineOverride == nil{preferences.set(language.rawValue,forKey:AppLanguage.preferenceKey)}}
 }
 @Published var glassShine:Double = ConverterModel.defaultGlassShine {
  didSet{
   let safe=ConverterModel.sanitizeAmount(glassShine,default:ConverterModel.defaultGlassShine,lo:0,hi:1)
   if safe != glassShine{glassShine=safe;return}
   preferences.set(safe,forKey:ConverterModel.glassShineKey)
  }
 }
 private let preferences:UserDefaults
 private var process:Process?; private var task:Task<Void,Never>?; private var stopped=false
 let root:URL
 static let upmixCentreKey   = "UTUVOConvert.upmix.centre"
 static let upmixSurroundKey = "UTUVOConvert.upmix.surround"
 static let upmixHeightKey   = "UTUVOConvert.upmix.height"
 static let upmixLayoutKey   = "UTUVOConvert.upmix.layout"
 static let glassShineKey = "UTUVOConvert.appearance.glassShine"
 static let defaultGlassShine = 0.75

 /// Sanitise a persisted amount: defaults if missing/invalid/nonfinite.
 /// The value is clamped into the layout's effective range so downstream
 /// sliders cannot re-introduce out-of-range values.
 static func sanitizeAmount(_ value:Double?, default fallback:Double, lo:Double, hi:Double) -> Double {
  guard let raw = value, raw.isFinite else { return fallback }
  if raw < lo { return lo }
  if raw > hi { return hi }
  return raw
 }
 /// Sanitise a persisted layout id: returns the default if missing /
 /// not present in the implemented list.
 static func sanitizeUpmixLayout(_ id:String?) -> String {
  guard let id = id, !id.isEmpty, UpmixLayoutCatalogue.layout(for: id) != nil else {
   return UpmixLayoutCatalogue.v2DefaultLayoutId
  }
  return id
 }
 /// Resolve the selected generic-v2 layout. A wheel petal supplies its
 /// direct layout before it becomes the active target; once active, the
 /// Advanced picker is authoritative.
 func resolvedUpmixLayout(forTarget choice:String) -> UpmixLayoutChoice? {
  guard choice.hasPrefix("upmix-") else{return nil}
  if choice == target { return UpmixLayoutCatalogue.layout(for:upmixLayout) }
  let id=String(choice.dropFirst("upmix-".count))
  if id=="5.1" { return UpmixLayoutCatalogue.v1TrialLayout }
  return UpmixLayoutCatalogue.layout(for:id)
 }
 init(engineRoot:URL?=nil) {
  if let i=CommandLine.arguments.firstIndex(of:"--preferences-suite"),i+1<CommandLine.arguments.count,let isolated=UserDefaults(suiteName:CommandLine.arguments[i+1]){preferences=isolated}else{preferences = .standard}
  language=AppLanguage.initial(preferences:preferences)
  glassShine=ConverterModel.sanitizeAmount(preferences.object(forKey:ConverterModel.glassShineKey) as? Double,default:ConverterModel.defaultGlassShine,lo:0,hi:1)
  let packaged=Bundle.main.resourceURL?.appendingPathComponent("OfflineEngine")
  root=engineRoot ?? packaged.flatMap{FileManager.default.fileExists(atPath:$0.appendingPathComponent("backend/worker.py").path) ? $0.resolvingSymlinksInPath():nil} ?? Bundle.main.bundleURL.deletingLastPathComponent().deletingLastPathComponent()
  UpmixLayoutCatalogue.configure(engineRoot:root)
  // Hydrate persisted upmix state. Sanitisation guarantees missing,
  // invalid or non-finite values fall back to the catalogue defaults.
  let ranges = UpmixLayoutCatalogue.amountRanges
  upmixCentre   = ConverterModel.sanitizeAmount(preferences.object(forKey:ConverterModel.upmixCentreKey)   as? Double, default: ranges.centre.default,   lo: ranges.centre.min,   hi: ranges.centre.max)
  upmixSurround = ConverterModel.sanitizeAmount(preferences.object(forKey:ConverterModel.upmixSurroundKey) as? Double, default: ranges.surround.default, lo: ranges.surround.min, hi: ranges.surround.max)
  upmixHeight   = ConverterModel.sanitizeAmount(preferences.object(forKey:ConverterModel.upmixHeightKey)   as? Double, default: ranges.height.default,   lo: ranges.height.min,   hi: ranges.height.max)
  upmixLayout   = ConverterModel.sanitizeUpmixLayout(preferences.string(forKey:ConverterModel.upmixLayoutKey))
  if let index=CommandLine.arguments.firstIndex(of:"--fixtures"),index+1<CommandLine.arguments.count {
   let dir=CommandLine.arguments[index+1]; Task { await add([URL(fileURLWithPath:dir)]) }
  }
 }
 func text(_ key:String)->String{language.text(key)}
 func formatText(_ key:String,_ values:String...)->String{
  if language == .english,values.first=="1"{if key=="{0} 個檔案"{return "1 file"};if key=="{0} 個音檔"{return "1 FILE"}}
  var result=text(key);for (i,value) in values.enumerated(){result=result.replacingOccurrences(of:"{\(i)}",with:value)};return result
 }
 func statusText(_ status:String)->String{text(status=="完成" ? "已完成":status=="轉換中" ? "處理中":status)}
 func detailText(_ detail:String)->String{UIStrings.diagnostic(detail,language:language)}
 func chooseFiles() { let p=NSOpenPanel();p.title=text("選擇音檔或資料夾");p.prompt=text("加入");p.canChooseFiles=true;p.canChooseDirectories=true;p.allowsMultipleSelection=true;if p.runModal() == .OK { Task{await add(p.urls)} } }
 func chooseOutput() {
  guard !running,!choosingOutput else{return}
  choosingOutput=true;defer{choosingOutput=false}
  let p=NSOpenPanel();p.canChooseFiles=false;p.canChooseDirectories=true;p.canCreateDirectories=true
  p.title=text("選擇輸出目錄");p.prompt=text("選擇")
  if !outDir.isEmpty{p.directoryURL=URL(fileURLWithPath:outDir)}
  if p.runModal() == .OK,let url=p.url{outDir=url.path}
 }
 var availableDepths:[String]{switch format{case "MP3","OGG":return ["24"];case "FLAC":return ["16","24"];case "AIFF":return ["16","24","32"];default:return ["16","24","32","float32"]}}
 func setFormat(_ value:String){format=value;if !availableDepths.contains(depth) || lossyFormat{depth="24"};if value=="MP3" && rate != "0" && !availableRates.contains(rate){rate="0"}}
 var pendingJobs:[AudioJob]{jobs.filter{!$0.info.isEmpty && $0.output==nil}}
 var resultSummary:String{
  let done=jobs.filter{$0.output != nil}.count,failed=jobs.filter{$0.status=="失敗" || $0.status=="不支援" || $0.status=="未轉換"}.count,cancelled=jobs.filter{$0.status=="已取消"}.count
  return formatText("完成 {0}",String(done))+(failed>0 ? " · "+formatText("失敗／不支援 {0}",String(failed)):"")+(cancelled>0 ? " · "+formatText("已取消 {0}",String(cancelled)):"")
 }
 /// Effective upmix centre for the given layout id. Quad has no centre
 /// channel so the centre amount is forced to 0; for every other layout
 /// the user's captured slider value is used (already sanitised).
 func effectiveCentre(forLayout layout:UpmixLayoutChoice) -> Double {
  return layout.hasCentre ? upmixCentre : 0.0
 }
 /// Effective upmix height for the given layout id. Non-height layouts
 /// always emit a silent height layer, so the amount is forced to 0.
 func effectiveHeight(forLayout layout:UpmixLayoutChoice) -> Double {
  return layout.hasHeight ? upmixHeight : 0.0
 }
 /// Effective upmix surround — every implemented layout has surround
 /// pairs, so the captured slider value passes through unmodified.
 func effectiveSurround() -> Double { upmixSurround }
 func issue(for job:AudioJob,target choice:String)->String?{
  let adm=job.info["adm"] as? Bool == true
  if let summary=job.info["summary"] as? [String:Any],let programmes=summary["programmes"] as? [[String:String]],programmes.count>1,!programmes.contains(where:{$0["id"]==job.programme}){return "請在明細選擇 ADM Programme。"}
  var counts=["mono":1,"stereo":2]
  for layout in UpmixLayoutCatalogue.sourceLayouts { counts[layout.id]=layout.channels }
  let isUpmixV2=choice.hasPrefix("upmix-")
  let upmixLayoutChoice:UpmixLayoutChoice? = resolvedUpmixLayout(forTarget: choice)
  let isUpmixRequest = isUpmixV2 && upmixLayoutChoice != nil
  if isUpmixRequest{
   if adm{return text("Upmix 僅支援 PCM 立體聲。")}
   if job.layout != "stereo" || (job.info["channels"] as? Int ?? 0) != 2 {return text("Upmix 僅支援 PCM 立體聲。")}
   guard let layout=upmixLayoutChoice else{return text("Upmix 需選已實作排列")}
   if !(0.0...1.0).contains(upmixSurround){return text("Upmix 請選範圍在 0–1 的數值。")}
   if layout.hasCentre && !(0.0...0.7).contains(upmixCentre){return text("Upmix 中心請選範圍在 0–0.7 的數值。")}
   if layout.hasHeight && !(0.0...0.5).contains(upmixHeight){return text("Upmix 高度範圍 0–0.5")}
   if let outputRate=rate == "0" ? job.info["sample_rate"] as? Int:Int(rate),!UpmixLayoutCatalogue.supportedRates.contains(outputRate){return text("Upmix 請選支援的取樣率：44.1、48、88.2、96、176.4 或 192 kHz。")}
  }
  if !adm{
   guard let count=counts[job.layout],count==job.info["channels"] as? Int else{return "請在明細指定與聲道數一致的 Source layout。"}
   if let declared=job.info["layout"] as? String,declared != "unknown",declared != job.layout{return "Source layout 與檔案的聲道標記不同。"}
  }
  let target=choice=="same" ? job.layout:choice
  let resolved:String
  if let layout = upmixLayoutChoice { resolved = layout.id }
  else { resolved = target }
  if adm && !["stereo","5.1-side","7.1.4-rear-first","binaural"].contains(resolved){return "ADM 請選 Stereo、5.1、7.1.4 或 Binaural。"}
  if !adm && resolved=="binaural" && !["stereo","5.1-side","7.1.4-side-first","7.1.4-rear-first"].contains(job.layout){return "Binaural 支援 stereo、5.1-side 與明確 7.1.4。"}
  // Only explicitly known layouts may be matrix-downmixed to Stereo.
  // Other ordinary PCM layout changes remain refused.
  if !isUpmixRequest && !adm && resolved != "binaural" && resolved != job.layout && resolved != "stereo"{return "一般 PCM 支援保留排列或已知聲道排列 → Stereo。"}
  let channels:Int
  if resolved=="binaural" { channels = 2 }
  else if let layout = UpmixLayoutCatalogue.layout(for: resolved) { channels = layout.channels }
  else { channels = counts[resolved] ?? 0 }
  if format=="OGG" && channels>2{return "OGG 限 mono／stereo；請選 Stereo 或 Binaural。"}
  if format=="MP3" && channels>2{return "MP3 限 mono／stereo；請選 Stereo 或 Binaural。"}
  if format=="MP3" && ![128,192,256,320].contains(mp3Bitrate){return "MP3 請選 128、192、256 或 320 kbps。"}
  if format=="MP3" && rate != "0" && !availableRates.contains(rate){return "MP3 請選 44.1 或 48 kHz。"}
  if format=="FLAC" && channels>8{return "FLAC 最多 8 聲道；7.1.4 請選 WAV 或 AIFF。"}
  if !availableDepths.contains(depth){return "請選此格式支援的 PCM 位深。"}
  return nil
 }
 func issue(for target:String)->String?{
  guard !pendingJobs.isEmpty else{return "沒有待轉換的有效音檔。"}
  for job in pendingJobs{if let reason=issue(for:job,target:target){return job.name+"："+reason}}
  return nil
 }
 func remove(_ id:UUID){guard !running else{return};jobs.removeAll{$0.id==id}}
 func expand(_ urls:[URL])->[String] {
  let extensions=Set(["wav","bwf","bw64","aif","aiff","flac","ogg","mp3","caf"])
  var paths=[String]()
  for url in urls {
   var dir:ObjCBool=false
   if FileManager.default.fileExists(atPath:url.path,isDirectory:&dir),dir.boolValue {
    if let e=FileManager.default.enumerator(at:url,includingPropertiesForKeys:[.isRegularFileKey],options:[.skipsHiddenFiles]) {
     for case let file as URL in e where extensions.contains(file.pathExtension.lowercased()) { paths.append(file.path) }
    }
   } else {paths.append(url.path)}
  }
  return Array(Set(paths.map{URL(fileURLWithPath:$0).standardizedFileURL.resolvingSymlinksInPath().path})).sorted()
 }
 func request(_ dict:[String:Any],handler:@escaping @MainActor ([String:Any])->Void) async throws {
  let p=Process(),input=Pipe(),output=Pipe()
  p.executableURL=root.appendingPathComponent(".runtime/bin/python")
  p.arguments=[root.appendingPathComponent("backend/worker.py").path]
  p.currentDirectoryURL=root;p.standardInput=input;p.standardOutput=output
  // The bundle is an immutable code/resource location, not a writable diagnostic directory.
  let errURL=FileManager.default.temporaryDirectory.appendingPathComponent("utuvo-convert-worker-"+UUID().uuidString+".stderr")
  defer{try? FileManager.default.removeItem(at:errURL)}
  FileManager.default.createFile(atPath:errURL.path,contents:nil)
  let err=try FileHandle(forWritingTo:errURL);p.standardError=err
  p.environment=ProcessInfo.processInfo.environment.merging(["PYTHONUNBUFFERED":"1"]){_,n in n}
  let payload=try JSONSerialization.data(withJSONObject:dict)
  defer{try? input.fileHandleForWriting.close();try? output.fileHandleForReading.close();try? err.close();if process===p{process=nil}}
  try p.run();process=p
  try input.fileHandleForWriting.write(contentsOf:payload);try input.fileHandleForWriting.close()
  // Pipe reading is isolated from UI; decoding/events return to main actor.
  try await withCheckedThrowingContinuation { (continuation:CheckedContinuation<Void,Error>) in
   DispatchQueue.global(qos:.userInitiated).async {
    var buffer=Data()
    func deliver(_ line:Data){if let value=try? JSONSerialization.jsonObject(with:line) as? [String:Any]{DispatchQueue.main.async{handler(value)}}}
    while true {
     let data=output.fileHandleForReading.availableData
     if data.isEmpty {break};buffer.append(data)
     while let newline=buffer.firstIndex(of:10) {
      let line=Data(buffer[..<newline]);buffer.removeSubrange(...newline)
      deliver(line)
     }
    }
    if !buffer.isEmpty{deliver(buffer)}
    p.waitUntilExit();try? err.close()
    DispatchQueue.main.async { continuation.resume() }
   }
  }
  if p.terminationStatus != 0 && !stopped{
   throw NSError(domain:"UTUVOConvert.Worker",code:Int(p.terminationStatus),userInfo:[NSLocalizedDescriptionKey:"離線引擎未完成工作（\(p.terminationStatus)）。請查看明細後重試。"])
  }
 }
 func add(_ urls:[URL]) async {
  guard !running,!choosingOutput else{return}
  let paths=expand(urls).filter{path in !jobs.contains{$0.path==path}}
  for path in paths {jobs.append(AudioJob(path:path,name:URL(fileURLWithPath:path).lastPathComponent))}
  guard !paths.isEmpty else{return};stopped=false;running=true;analysing=true;notice="分析 \(paths.count) 個檔案…"
  var failure:String?
  do {try await request(["action":"probe","paths":paths]) { event in
   if let info=event["info"] as? [String:Any],let path=info["path"] as? String,let i=self.jobs.firstIndex(where:{$0.path==path}) {
    self.jobs[i].info=info;self.jobs[i].layout=info["layout"] as? String ?? "unknown";self.jobs[i].status="就緒"
    let ch=info["channels"] as? Int ?? 0,sr=info["sample_rate"] as? Int ?? 0,seconds=info["duration"] as? Double ?? 0
    let adm=info["adm"] as? Bool ?? false, summary=info["summary"] as? [String:Any] ?? [:]
    let objects=summary["object_channels"] as? Int ?? 0
    let programmes=summary["programmes"] as? [[String:String]] ?? []
    if programmes.count>1 {self.jobs[i].status="需選 Programme"}
    self.jobs[i].detail="\(info["format"] ?? "") · \(info["subtype"] ?? "") · \(ch) ch · \(sr) Hz · \(String(format:"%.2f",seconds)) s"+(adm ? " · ADM \(objects) object ch":"")
   } else if let path=event["path"] as? String,let i=self.jobs.firstIndex(where:{$0.path==path}) { self.jobs[i].status="不支援";self.jobs[i].detail=event["error"] as? String ?? "無法讀取" }
  }} catch {failure=error.localizedDescription}
  for i in jobs.indices where paths.contains(jobs[i].path) && jobs[i].status=="待分析"{jobs[i].status=stopped ? "已取消":"不支援";jobs[i].detail=stopped ? "分析已取消，可移除後重新加入。":failure ?? "離線引擎沒有傳回分析結果。"}
  analysing=false;running=false;notice=stopped ? "分析已取消；已分析檔案保留。":failure ?? "\(jobs.count) 個檔案；\(jobs.filter{!$0.info.isEmpty}.count) 個可轉換。"
 }
 func start() {
  guard !running,!choosingOutput,!outDir.isEmpty,issue(for:target)==nil else{return};stopped=false;overall=0;running=true
  // Capture every per-batch parameter synchronously BEFORE creating the
  // Task; subsequent edits to the published sliders / format / depth /
  // upmix layout / upmix amounts must not mutate the request mid-run.
  let capturedTarget=target
  let capturedUpmixLayout=upmixLayout
  let capturedRate=Int(rate) ?? 0
  let capturedCentre=upmixCentre
  let capturedSurround=upmixSurround
  let capturedHeight=upmixHeight
  let capturedFormat=format
  let capturedDepth=depth
  let capturedMP3Bitrate=mp3Bitrate
  let capturedOutDir=outDir
  // Every App Upmix selection uses generic v2. The old v1 request mode
  // remains available to existing backend/API callers.
  let capturedV2Layout:UpmixLayoutChoice? = capturedTarget.hasPrefix("upmix-")
   ? UpmixLayoutCatalogue.layout(for:capturedUpmixLayout) : nil
  task=Task {
   let ids=pendingJobs.map(\.id)
   for i in jobs.indices where ids.contains(jobs[i].id){jobs[i].status="排隊中";jobs[i].progress=0}
   for (n,id) in ids.enumerated() {
    if stopped || Task.isCancelled {break}
    guard let i=jobs.firstIndex(where:{$0.id==id}) else{continue}
    let job=jobs[i];jobs[i].status="轉換中";jobs[i].progress=0;notice=job.name
    let resolvedTarget=capturedTarget=="same" ? job.layout : capturedTarget
    var req:[String:Any]=["action":"convert","input":job.path,"layout":job.layout,"programme":job.programme,"out_dir":capturedOutDir,"format":capturedFormat,"depth":capturedDepth,"target":resolvedTarget,"rate":capturedRate]
    if capturedFormat == "MP3"{req["mp3_bitrate"]=capturedMP3Bitrate}
    if let layout = capturedV2Layout{
     // The direct petal and Advanced picker share this captured layout.
     // Centre / height are effective zero when the layout has no such
     // channels, without changing the saved user preference.
     req["target"]=layout.id
     req["upmix"]=[
      "mode":"upmix-generic-v2",
      "layout":layout.id,
      "centre":layout.hasCentre ? capturedCentre : 0.0,
      "surround":capturedSurround,
      "height":layout.hasHeight ? capturedHeight : 0.0,
     ] as [String:Any]
    }
    do {try await request(req) {event in
     guard let idx=self.jobs.firstIndex(where:{$0.id==id}) else{return}
     switch event["event"] as? String {
     case "progress":self.jobs[idx].progress=event["value"] as? Double ?? 0;self.overall=(Double(n)+self.jobs[idx].progress)/Double(max(1,ids.count))
     case "done":
      let r=event["report"] as? [String:Any] ?? [:]
      guard let output=r["output"] as? String,FileManager.default.fileExists(atPath:output) else{self.jobs[idx].status="失敗";self.jobs[idx].detail="離線引擎沒有提供可用的輸出檔。";return}
      self.jobs[idx].status="完成";self.jobs[idx].output=output;self.jobs[idx].progress=1
      let labels=(r["labels"] as? [String] ?? []).joined(separator:" ")
      self.jobs[idx].detail="QC PASS · \(String(format:"%.3f",r["duration"] as? Double ?? 0)) s · \(r["sample_rate"] ?? "") Hz · \(labels)"
      if let warnings=r["warnings"] as? [String],!warnings.isEmpty{self.jobs[idx].detail += "\n⚠ "+warnings.joined(separator:"\n")}
     case "failed":self.jobs[idx].status="失敗";self.jobs[idx].detail=event["error"] as? String ?? "轉換失敗"
     default:break
     }
    }} catch {if jobs[i].status=="轉換中"{jobs[i].status="失敗";jobs[i].detail=error.localizedDescription}}
    if stopped && jobs[i].output == nil {jobs[i].status="已取消";jobs[i].detail="未完成輸出未發佈；可重試。"} else if jobs[i].status=="轉換中" {jobs[i].status="失敗";jobs[i].detail="離線引擎沒有傳回轉換結果。"}
    overall=Double(n+1)/Double(max(1,ids.count))
   }
   for i in jobs.indices where jobs[i].status=="排隊中"{jobs[i].status="已取消";jobs[i].detail="尚未開始；可重試。"}
   running=false;task=nil;notice=resultSummary+(stopped ? "。完成的檔案保留。":"。")
  }
 }
 func cancel(){stopped=true;task?.cancel();if let process,process.isRunning{process.terminate()}}
 func reveal(_ path:String){NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath:path)])}
}
