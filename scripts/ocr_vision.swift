import AppKit
import Foundation
import Vision

struct Hit: Codable {
    let text: String
    let confidence: Float
    let x: Double
    let y: Double
    let width: Double
    let height: Double
}

struct ImageResult: Codable {
    let path: String
    let width: Int
    let height: Int
    let hits: [Hit]
    let elapsed_seconds: Double
}

func recognize(path: String) throws -> ImageResult {
    let started = Date()
    guard let image = NSImage(contentsOfFile: path),
          let tiff = image.tiffRepresentation,
          let bitmap = NSBitmapImageRep(data: tiff),
          let cgImage = bitmap.cgImage else {
        throw NSError(domain: "VisionOCR", code: 1, userInfo: [NSLocalizedDescriptionKey: "无法读取图片：\(path)"])
    }

    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.recognitionLanguages = ["zh-Hans", "en-US"]
    request.usesLanguageCorrection = true
    try VNImageRequestHandler(cgImage: cgImage, options: [:]).perform([request])

    let hits = (request.results ?? []).compactMap { observation -> Hit? in
        guard let candidate = observation.topCandidates(1).first else { return nil }
        let box = observation.boundingBox
        return Hit(
            text: candidate.string,
            confidence: candidate.confidence,
            x: box.minX,
            y: box.minY,
            width: box.width,
            height: box.height
        )
    }

    return ImageResult(
        path: path,
        width: cgImage.width,
        height: cgImage.height,
        hits: hits,
        elapsed_seconds: Date().timeIntervalSince(started)
    )
}

do {
    let paths = Array(CommandLine.arguments.dropFirst())
    if paths.isEmpty {
        throw NSError(domain: "VisionOCR", code: 2, userInfo: [NSLocalizedDescriptionKey: "至少提供一张图片"])
    }
    let results = try paths.map(recognize)
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys]
    FileHandle.standardOutput.write(try encoder.encode(results))
} catch {
    FileHandle.standardError.write(Data((error.localizedDescription + "\n").utf8))
    exit(1)
}
