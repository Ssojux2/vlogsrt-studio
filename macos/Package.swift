// swift-tools-version: 6.2
import PackageDescription

let package = Package(
    name: "FinalCutAgent",
    platforms: [.macOS(.v14)],
    products: [
        .library(name: "FinalCutAgentCore", targets: ["FinalCutAgentCore"]),
        .library(name: "WorkflowExtension", targets: ["WorkflowExtension"]),
        .executable(name: "FinalCutAgentApp", targets: ["FinalCutAgentApp"]),
    ],
    targets: [
        .target(
            name: "FinalCutAgentCore",
            linkerSettings: [.linkedFramework("AppKit"), .linkedFramework("Security")]
        ),
        .target(name: "WorkflowExtension", dependencies: ["FinalCutAgentCore"]),
        .executableTarget(name: "FinalCutAgentApp", dependencies: ["FinalCutAgentCore"]),
        .testTarget(name: "FinalCutAgentCoreTests", dependencies: ["FinalCutAgentCore"]),
        .testTarget(name: "WorkflowExtensionTests", dependencies: ["WorkflowExtension"]),
    ]
)

