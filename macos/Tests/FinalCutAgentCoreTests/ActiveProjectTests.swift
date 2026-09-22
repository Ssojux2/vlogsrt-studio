import Foundation
import Testing

@testable import FinalCutAgentCore

private let active = ActiveProjectSelection(
  projectUID: "ACTIVE-1",
  projectName: "My Project",
  timelineStart: RationalTime(value: 3_600, timescale: 1),
  duration: RationalTime(value: 120, timescale: 1),
  frameDuration: RationalTime(value: 1, timescale: 25)
)

private func fcpxml(uid: String?, name: String = "My Project", extraProject: Bool = false) -> Data {
  let uidAttribute = uid.map { " uid=\"\($0)\"" } ?? ""
  let extra = extraProject ? "<project name=\"Other\" uid=\"OTHER\"><sequence/></project>" : ""
  return Data(
    """
    <?xml version="1.0" encoding="UTF-8"?>
    <fcpxml version="1.14"><library><event>
      <project name="\(name)"\(uidAttribute)><sequence/></project>\(extra)
    </event></library></fcpxml>
    """.utf8
  )
}

@Test func droppedProjectMustMatchTheTimelineProjectUID() throws {
  let validated = try FCPXMLProjectGate.validate(fcpxml(uid: "ACTIVE-1"), activeProject: active)
  #expect(validated.projectUID == "ACTIVE-1")
  #expect(validated.projectName == "My Project")

  #expect(throws: ActiveProjectError.self) {
    try FCPXMLProjectGate.validate(
      fcpxml(uid: "OTHER", name: "My Project"),
      activeProject: active
    )
  }
}

@Test func missingActiveProjectUIDAndMultipleProjectsFailClosed() {
  #expect(throws: ActiveProjectError.self) {
    try FCPXMLProjectGate.validate(fcpxml(uid: "ACTIVE-1"), activeProject: nil)
  }
  #expect(throws: ActiveProjectError.self) {
    try FCPXMLProjectGate.validate(fcpxml(uid: nil), activeProject: active)
  }
  #expect(throws: ActiveProjectError.self) {
    try FCPXMLProjectGate.validate(
      fcpxml(uid: "ACTIVE-1", extraProject: true),
      activeProject: active
    )
  }
}

@Test func rationalTimeRejectsInvalidTimelineValues() {
  #expect(throws: ActiveProjectError.self) {
    try ActiveProjectSelection.validated(
      projectUID: "P",
      projectName: "Project",
      timelineStart: RationalTime(value: -1, timescale: 1),
      duration: RationalTime(value: 1, timescale: 1),
      frameDuration: RationalTime(value: 1, timescale: 25)
    )
  }
  #expect(throws: ActiveProjectError.self) {
    try ActiveProjectSelection.validated(
      projectUID: "P",
      projectName: "Project",
      timelineStart: RationalTime(value: 0, timescale: 1),
      duration: RationalTime(value: 0, timescale: 1),
      frameDuration: RationalTime(value: 1, timescale: 25)
    )
  }
}
