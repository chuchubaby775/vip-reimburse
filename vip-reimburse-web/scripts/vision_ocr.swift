import Foundation
import AppKit
import Vision
import PDFKit

// Offline fallback. Coordinates are normalized with origin at the image's top left.
struct OCRLine: Codable { let text: String; let confidence: Float }
struct OCRBlock: Codable { let text: String; let confidence: Float; let x: CGFloat; let y: CGFloat; let width: CGFloat; let height: CGFloat; let page: Int }
struct OCRResult: Codable { let engine: String; let lines: [OCRLine]; let blocks: [OCRBlock] }
func recognize(_ image: CGImage, page: Int) throws -> ([OCRLine], [OCRBlock]) {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.recognitionLanguages = ["zh-Hans", "en-US"]
    request.usesLanguageCorrection = false
    try VNImageRequestHandler(cgImage: image, options: [:]).perform([request])
    let blocks: [OCRBlock] = (request.results ?? []).compactMap { item in
        guard let candidate = item.topCandidates(1).first else { return nil }
        let rect = item.boundingBox
        return OCRBlock(text: candidate.string, confidence: candidate.confidence, x: rect.minX, y: 1 - rect.maxY, width: rect.width, height: rect.height, page: page)
    }.sorted { $0.y < $1.y }
    var rows: [[OCRBlock]] = []
    for item in blocks {
        if let idx = rows.firstIndex(where: { abs(($0.first?.y ?? 0) - item.y) < max(item.height * 0.65, 0.006) }) { rows[idx].append(item) }
        else { rows.append([item]) }
    }
    let lines = rows.map { row in
        let ordered = row.sorted { $0.x < $1.x }
        return OCRLine(text: ordered.map { $0.text }.joined(separator: " "), confidence: ordered.map { $0.confidence }.min() ?? 0)
    }
    return (lines, blocks)
}
do {
    guard CommandLine.arguments.count >= 2 else { throw NSError(domain: "OCR", code: 1) }
    let url = URL(fileURLWithPath: CommandLine.arguments[1])
    var lines: [OCRLine] = []
    var blocks: [OCRBlock] = []
    if CommandLine.arguments.count > 2 && CommandLine.arguments[2] == "application/pdf" {
        guard let doc = PDFDocument(url: url), doc.pageCount <= 30 else { throw NSError(domain: "PDF", code: 2) }
        for index in 0..<min(doc.pageCount, 5) {
            guard let page = doc.page(at: index) else { continue }
            let bounds = page.bounds(for: .mediaBox)
            let size = NSSize(width: min(bounds.width * 2.5, 2200), height: min(bounds.height * 2.5, 3100))
            let image = page.thumbnail(of: size, for: .mediaBox)
            var rect = CGRect(origin: .zero, size: image.size)
            if let cg = image.cgImage(forProposedRect: &rect, context: nil, hints: nil) {
                let result = try recognize(cg, page: index)
                lines += result.0; blocks += result.1
            }
        }
    } else {
        guard let image = NSImage(contentsOf: url) else { throw NSError(domain: "Image", code: 3) }
        var rect = CGRect(origin: .zero, size: image.size)
        guard let cg = image.cgImage(forProposedRect: &rect, context: nil, hints: nil) else { throw NSError(domain: "Image", code: 4) }
        let result = try recognize(cg, page: 0)
        lines = result.0; blocks = result.1
    }
    let data = try JSONEncoder().encode(OCRResult(engine: "Apple Vision（本机离线）", lines: lines, blocks: blocks))
    print(String(data: data, encoding: .utf8)!)
} catch {
    FileHandle.standardError.write(Data("本机离线识别失败\n".utf8))
    exit(1)
}
