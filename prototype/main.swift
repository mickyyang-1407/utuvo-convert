import Foundation
// File-only adapter. No audio graph, HAL enumeration or device output.
let args = CommandLine.arguments
if args.count != 4 { fatalError("mix-helper input.raw output.raw matrix.json") }
let spec = try JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: args[3]))) as! [String: Any]
let matrix = spec["matrix"] as! [[Double]] // [out][in]
let channels = matrix[0].count
let raw = try Data(contentsOf: URL(fileURLWithPath: args[1]))
let samples: [Float] = raw.withUnsafeBytes { b in
    (0..<(b.count / 4)).map { Float(bitPattern: UInt32(littleEndian: b.loadUnaligned(fromByteOffset: $0 * 4, as: UInt32.self))) }
}
precondition(samples.count % channels == 0)
let frames = samples.count / channels
let planar = (0..<channels).map { c -> UnsafeMutablePointer<Float> in
    let p = UnsafeMutablePointer<Float>.allocate(capacity: max(1, frames))
    for f in 0..<frames { p[f] = samples[f * channels + c] }
    return p
}
defer { planar.forEach { $0.deallocate() } }
var output = [Float](repeating: 0, count: frames * matrix.count)
planar.withUnsafeBufferPointer { planes in
    for (out, row) in matrix.enumerated() {
        let weights = row.map(Float.init)
        weights.withUnsafeBufferPointer { w in
            for f in 0..<frames {
                output[f * matrix.count + out] = MixingPrecision.weightedSum(planar: planes.baseAddress!, frame: f, weights: w.baseAddress!, channels: channels, precise: true)
            }
        }
    }
}
let data = output.withUnsafeBytes { Data($0) }
try data.write(to: URL(fileURLWithPath: args[2]), options: .withoutOverwriting)
