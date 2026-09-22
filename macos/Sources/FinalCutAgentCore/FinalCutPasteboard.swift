import AppKit

public enum FinalCutPasteboard {
  public static let supportedTypeNames = [
    "com.apple.finalcutpro.xml.v1-14",
    "com.apple.finalcutpro.xml.v1-13",
    "com.apple.finalcutpro.xml",
  ]

  public static let supportedTypes = supportedTypeNames.map { NSPasteboard.PasteboardType($0) }

  public static func bestData(on pasteboard: NSPasteboard) -> Data? {
    for type in supportedTypes where pasteboard.availableType(from: [type]) != nil {
      if let data = pasteboard.data(forType: type) { return data }
    }
    return nil
  }
}
