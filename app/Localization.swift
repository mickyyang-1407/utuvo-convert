import Foundation

enum AppLanguage:String,CaseIterable {
 case traditionalChinese="zh-Hant",english="en"
 var name:String{self == .traditionalChinese ? "繁體中文":"English"}
 static let preferenceKey="UTUVOConvert.language"
 static var commandLineOverride:AppLanguage?{
  guard let i=CommandLine.arguments.firstIndex(of:"--language"),i+1<CommandLine.arguments.count else{return nil}
  return AppLanguage(rawValue:CommandLine.arguments[i+1])
 }
 static func initial(preferences:UserDefaults = .standard)->AppLanguage{
  commandLineOverride ?? preferences.string(forKey:preferenceKey).flatMap(AppLanguage.init(rawValue:)) ?? .traditionalChinese
 }
 func text(_ source:String)->String{
  if let pair=UIStrings.lookup[source]{return self == .traditionalChinese ? pair.0:pair.1}
  if source.hasPrefix("離線引擎未完成工作（"),let end=source.firstIndex(of:"）"){
   let start=source.index(source.startIndex,offsetBy:"離線引擎未完成工作（".count)
   return format("離線引擎未完成工作（{0}）。請查看明細後重試。",String(source[start..<end]))
  }
  // Stored job/flow diagnostics remain canonical. Changing language also updates
  // existing messages, while filenames and unrecognised engine diagnostics stay intact.
  for (prefix,pair) in UIStrings.prefixes where source.hasPrefix(prefix){
   let rest=String(source.dropFirst(prefix.count))
   return (self == .traditionalChinese ? pair.0:pair.1)+text(rest)
  }
  for (key,pair) in UIStrings.suffixed where source.hasSuffix(key){
   let head=String(source.dropLast(key.count))
   return (self == .english && head.hasSuffix("：") ? String(head.dropLast())+": ":head)+(self == .traditionalChinese ? pair.0:pair.1)
  }
  return source
 }
 func format(_ key:String,_ values:String...)->String{
  var result=text(key)
  for (i,value) in values.enumerated(){result=result.replacingOccurrences(of:"{\(i)}",with:value)}
  return result
 }
}

enum UIStrings {
 // All app-authored UI text, including accessibility and native menu/alert text.
 static let pairs:[(String,String)]=[
  ("MP3 已完成。解碼峰值超過 0 dBFS，播放可能削波；原音量未變，可降低來源音量後再轉。","MP3 completed. Decoded peaks exceed 0 dBFS and playback may clip; source level is unchanged. Reduce source gain for more headroom."),
  ("拖入音檔","Drop Audio"),("取消處理","Cancel processing"),("清除選擇","Clear selection"),
  ("加入音檔","Add audio"),("語言","Language"),("取消","Cancel"),("完成","Done"),
  ("進階","Advanced"),("設定","Settings"),("設定…","Settings…"),("玻璃光澤","Glass shine"),
  ("柔和","Soft"),("光亮","Glossy"),("恢復建議值","Reset to recommended"),
  ("拖動滑桿，直接預覽本體光澤。","Drag to preview the glass shine."),
  ("降低透明度已啟用","Reduce Transparency is enabled"),
  ("玻璃素材尚未就緒","Glass artwork is unavailable"),
  ("返回","Back"),("明細","Details"),("批次明細","Batch details"),
  ("重試未完成","Retry unfinished"),("再轉一批","New batch"),("輸出設定","Output settings"),
  ("取樣率","Sample rate"),("來源取樣率","Source rate"),("PCM 位深","PCM depth"),
  ("輸出資料夾","Output folder"),("選擇輸出目錄","Choose output folder"),
  ("選擇音檔或資料夾","Choose audio files or folders"),("加入","Add"),("選擇","Choose"),
  ("未指定","Unknown"),("來源聲道排列","Source layout"),("選擇節目","Select programme"),
  ("ADM 節目","ADM Programme"),("在 Finder 顯示輸出","Show output in Finder"),
  ("{0} 個檔案","{0} files"),("{0} 個音檔","{0} FILES"),("移除 {0}","Remove {0}"),
  ("選格式","Format"),("下一步 →","Next →"),("轉換","CONVERT"),
  ("轉換中","RENDERING"),("結果","RESULT"),("處理中…","Processing…"),
  ("分析音檔…","Analysing audio…"),("分析中","Analyse"),("1 · 選擇輸出格式","1 · Choose output format"),
  ("有損 · 128–320 kbps","Lossy · 128–320 kbps"),
  ("MP3 位元率","MP3 bitrate"),
  ("MP3 · 有損編碼 · 單／雙聲道","MP3 · Lossy · mono/stereo"),
  ("來源取樣率（MP3 44.1／48 kHz）","Source rate (MP3 44.1/48 kHz)"),
  ("來源為其他取樣率時，MP3 會轉為 48 kHz。","Other source sample rates are converted to 48 kHz for MP3."),
  ("MP3 限 mono／stereo；請選 Stereo 或 Binaural。","MP3 supports mono/stereo; choose Stereo or Binaural."),
  ("MP3 請選 128、192、256 或 320 kbps。","Choose 128, 192, 256 or 320 kbps for MP3."),
  ("MP3 請選 44.1 或 48 kHz。","Choose 44.1 or 48 kHz for MP3."),
  ("已選 {0} · 按下一步","{0} selected · Next"),("2 · 選擇聲道排列／渲染","2 · Choose layout / render"),
  ("保持原檔排列","Original"),("雙聲道","Stereo"),("雙耳渲染","Binaural"),
  ("保留來源聲道排列","Keep declared layout"),("無壓縮 PCM","Uncompressed PCM"),
  ("無損 · 最多 8 聲道","Lossless · ≤8 channels"),("Vorbis · 單／雙聲道","Vorbis · mono/stereo"),
  ("ADM → 雙聲道","ADM → 2 channels"),("側環繞 · 6 聲道","Side surrounds · 6 channels"),
  ("12 聲道 · WAV/AIFF","12 channels · WAV/AIFF"),("Apple HRTF · 雙聲道","Apple HRTF · stereo"),
  ("無法使用","Unavailable"),("不做正規化 · 保留原檔","Normalization off · Originals kept"),
  ("OGG Vorbis · 有損編碼 · 單／雙聲道","OGG Vorbis · Lossy · mono/stereo"),
  ("預設保留來源取樣率。不做正規化；降位深未加 dither。輸出附聲道與 QC 報告，原始 metadata 不複製。","Source rate is kept by default. Normalization is off; no dither on bit-depth reduction. Channel and QC reports are included; source metadata is not copied."),
  ("退出 UTUVO convert","Quit UTUVO convert"),("檔案","File"),("編輯","Edit"),
  ("加入音檔…","Add Audio…"),("顯示 Convert","Show Convert"),("剪下","Cut"),("複製","Copy"),
  ("貼上","Paste"),("全選","Select All"),("仍在處理音檔","Audio is still processing"),
  ("取消處理並離開？已完成的輸出會保留。","Cancel processing and quit? Completed outputs will be kept."),
  ("繼續處理","Keep processing"),("取消並離開","Cancel and quit"),
  ("待分析","Pending analysis"),("就緒","Ready"),("需選 Programme","Choose programme"),
  ("不支援","Unsupported"),("失敗","Failed"),("未轉換","Not converted"),("已取消","Cancelled"),
  ("排隊中","Queued"),("處理中","Converting"),("已完成","Completed"),
  ("完成 {0}","Completed {0}"),("失敗／不支援 {0}","Failed / unsupported {0}"),("已取消 {0}","Cancelled {0}"),
  ("拖入音檔或資料夾，加入批次清單。","Drop audio files or folders to add them to the batch."),
  ("此格式不支援此聲道配置。","This format does not support the selected channel layout."),
  ("部分檔案未就緒；開啟明細查看原因。","Some files are not ready; open Details for the reason."),
  ("未找到支援的音檔","No supported audio files found"),("取消中…","Cancelling…"),
  ("已取消選擇；原檔保留。","Selection cleared; originals are kept."),
  ("重試未完成項目；已完成輸出保留。","Retry unfinished items; completed outputs are kept."),
  ("正在處理音檔，請完成後再加入。","Audio is processing; add files when it finishes."),
  ("請在明細選擇 ADM Programme。","Choose an ADM programme in Details."),
  ("請在明細指定與聲道數一致的 Source layout。","Choose a source layout matching the channel count in Details."),
  ("Source layout 與檔案的聲道標記不同。","The source layout conflicts with the file's channel labels."),
  ("ADM 請選 Stereo、5.1、7.1.4 或 Binaural。","For ADM, choose Stereo, 5.1, 7.1.4 or Binaural."),
  ("Binaural 支援 stereo、5.1-side 與明確 7.1.4。","Binaural supports stereo, 5.1-side and explicit 7.1.4."),
  ("一般 PCM 支援保留排列或已知聲道排列 → Stereo。","PCM supports the original layout or a known speaker layout → Stereo."),
  ("OGG 限 mono／stereo；請選 Stereo 或 Binaural。","OGG supports mono/stereo; choose Stereo or Binaural."),
  ("FLAC 最多 8 聲道；7.1.4 請選 WAV 或 AIFF。","FLAC supports up to 8 channels; use WAV or AIFF for 7.1.4."),
  ("請選此格式支援的 PCM 位深。","Choose a PCM depth supported by this format."),
  ("沒有待轉換的有效音檔。","No valid audio files are waiting for conversion."),
  ("無法讀取","Cannot read audio"),("分析已取消，可移除後重新加入。","Analysis cancelled; remove and add the file again."),
  ("離線引擎沒有傳回分析結果。","The offline engine returned no analysis result."),
  ("離線引擎未完成工作（{0}）。請查看明細後重試。","The offline engine did not finish ({0}). Check Details and retry."),
  ("分析已取消；已分析檔案保留。","Analysis cancelled; analysed files are kept."),
  ("離線引擎沒有提供可用的輸出檔。","The offline engine returned no usable output file."),
  ("轉換失敗","Conversion failed"),("未完成輸出未發佈；可重試。","Incomplete output was not published; retry is available."),
  ("離線引擎沒有傳回轉換結果。","The offline engine returned no conversion result."),
  ("尚未開始；可重試。","Not started; retry is available."),
  ("目的地已有檔案；請選其他資料夾，現有檔案不會覆寫。","Destination already exists; choose another folder. Existing files will not be overwritten."),
  ("檔案無法解碼，請確認是支援的音訊格式。","The file cannot be decoded; check that it is a supported audio format."),
  ("此版本單檔解碼 PCM 上限為 512 MiB。","This build has a 512 MiB decoded PCM limit per file."),
  ("5.1 Upmix","5.1 Upmix"),
  ("試用","Trial"),
  ("Upmix 請選支援的取樣率：44.1、48、88.2、96、176.4 或 192 kHz。","Choose a supported Upmix sample rate: 44.1, 48, 88.2, 96, 176.4 or 192 kHz."),
  ("試用 · 環境聲延伸","Trial · ambience extension"),
  ("環繞強度","Surround"),
  ("中央聚焦","Centre"),
  ("此試用為估測之環境聲與中央聲，非原始環繞還原。","This trial is an estimate of ambience and centre, not a recovery of the original surround mix."),
  ("Stereo 轉 5.1 試用僅支援 PCM 立體聲；ADM 與其他排列請改選標準 5.1。","The stereo→5.1 trial is PCM-stereo only; choose standard 5.1 for ADM and other layouts."),
  ("Upmix 請選範圍在 0–1 的數值。","Upmix amounts must be in the 0–1 range."),
  ("Upmix 中心請選範圍在 0–0.7 的數值。","Upmix centre must be in the 0–0.7 range."),
  ("5.1 Upmix","5.1 Upmix"),
  ("7.1.4 Upmix","7.1.4 Upmix"),
  ("Upmix","Upmix"),
  ("單聲道","Mono"),
  ("4.0 四聲道","4.0 Quad"),
  ("5.0","5.0"),
  ("5.1 側環繞","5.1 Side"),
  ("5.1 後環繞","5.1 Rear"),
  ("7.0","7.0"),
  ("7.1","7.1"),
  ("5.1.2 前上方","5.1.2 Top Front"),
  ("5.1.4","5.1.4"),
  ("7.1.2 前上方","7.1.2 Top Front"),
  ("7.1.4","7.1.4"),
  ("7.1.4 側環繞先行（明確輸入）","7.1.4 Side First (explicit input)"),
  ("Upmix 排列","Upmix layout"),
  ("Upmix 高度","Upmix height"),
  ("Upmix 環繞","Upmix surround"),
  ("Upmix 中央","Upmix centre"),
  ("未支援","Not supported"),
  ("Upmix 需選已實作排列","Upmix requires an implemented layout."),
  ("Upmix 高度不適用","Height is not used for this layout."),
  ("Upmix 中央不適用","Centre is not used for this layout."),
  ("Upmix 環繞範圍 0–1","Surround amount (0–1)."),
  ("Upmix 中心範圍 0–0.7","Centre amount (0–0.7)."),
  ("Upmix 高度範圍 0–0.5","Height amount (0–0.5)."),
  ("Upmix 僅支援 PCM 立體聲。","Upmix requires PCM stereo input."),
  ("Upmix 已實作排列","Implemented Upmix layouts"),
  ("參考用排列 · 尚未支援","Reference-only · not yet supported"),
  ("Upmix 中央對此排列無效","Centre has no effect for this layout."),
  ("Upmix 高度對此排列無效","Height has no effect for this layout."),
  ("Upmix 請在進階選單選擇已實作排列。","Pick an implemented Upmix layout in Advanced.")
 ]
 static let lookup:[String:(String,String)]={var result=[String:(String,String)]();for pair in pairs{result[pair.0]=pair;result[pair.1]=pair};return result}()
 static let suffixed:[(String,(String,String))]=pairs.filter{$0.0.hasSuffix("。")}.flatMap{[($0.0,$0),($0.1,$0)]}.sorted{$0.0.count>$1.0.count}
 static let prefixes:[(String,(String,String))]=[
  ("無法讀取音檔：",("無法讀取音檔：","Cannot read audio: ")),
  ("Cannot read audio: ",("無法讀取音檔：","Cannot read audio: "))
 ]
 static func diagnostic(_ raw:String,language:AppLanguage)->String{
  let key:String?
  if raw.contains("overwrite forbidden"){key="目的地已有檔案；請選其他資料夾，現有檔案不會覆寫。"}
  else if raw.contains("MP3 decoded peak"){key="MP3 已完成。解碼峰值超過 0 dBFS，播放可能削波；原音量未變，可降低來源音量後再轉。"}
  else if raw.contains("512 MiB decoded PCM"){key="此版本單檔解碼 PCM 上限為 512 MiB。"}
  else if raw.contains("Format not recognised") || raw.contains("Format not recognized"){key="檔案無法解碼，請確認是支援的音訊格式。"}
  else{key=nil}
  return key.map{language.text($0)+"\n"+raw} ?? language.text(raw)
 }
}
