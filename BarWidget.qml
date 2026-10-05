import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

BarWidget {
  id: root
  moduleName: "oorestisime.ai-usage"

  property var snapshot: Model.emptySnapshot()
  property bool refreshing: false
  property string lastError: ""
  property string outputText: ""
  property string errorText: ""
  readonly property int refreshIntervalSec: Math.max(300, Math.min(3600, parseInt(String(setting("refreshIntervalSec", 900)), 10) || 900))
  readonly property bool showOpenCodeGo: setting("showOpenCodeGo", true) !== false
  readonly property var latestDay: Model.latestDay(snapshot)
  readonly property string displayText: latestDay ? Model.formatTokens(latestDay.tokens) : "—"

  function localPath(url) {
    return decodeURIComponent(String(url).replace("file://", ""))
  }

  function refresh() {
    if (collector.running) return
    root.outputText = ""
    root.errorText = ""
    root.lastError = ""
    root.refreshing = true
    var command = ["python3", localPath(Qt.resolvedUrl("scripts/collect.py"))]
    if (!root.showOpenCodeGo) command.push("--no-opencode-go")
    collector.command = command
    collector.running = true
  }

  readonly property bool opened: panelLoader.item ? panelLoader.item.opened === true : false
  readonly property bool popoutSwitchClosing: panelLoader.item ? panelLoader.item.popoutSwitchClosing === true : false

  function open() { if (panelLoader.item) panelLoader.item.open() }
  function close() { if (panelLoader.item) panelLoader.item.close() }
  function togglePanel() { if (panelLoader.item) panelLoader.item.toggle() }
  function closeForPopoutSwitch() { if (panelLoader.item) panelLoader.item.closeForPopoutSwitch() }

  function injectPanel() {
    if (!panelLoader.item) return
    panelLoader.item.bar = root.bar
    panelLoader.item.anchorItem = button
    panelLoader.item.hostWidget = root
    panelLoader.item.usage = root
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onBarChanged: injectPanel()

  Timer {
    interval: root.refreshIntervalSec * 1000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  Process {
    id: collector
    running: false
    command: []
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.outputText = text
    }
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.errorText = text
    }
    onExited: function(exitCode) {
      root.refreshing = false
      if (exitCode !== 0) {
        root.lastError = String(root.errorText || root.outputText || "Collector failed").trim()
        return
      }
      try {
        root.snapshot = Model.parseSnapshot(root.outputText)
        root.lastError = ""
      } catch (error) {
        root.lastError = error && error.message ? error.message : "Could not read usage"
      }
    }
  }

  Loader {
    id: panelLoader
    active: true
    source: Qt.resolvedUrl("Panel.qml")
    visible: false
    onLoaded: {
      root.injectPanel()
      Qt.callLater(root.injectPanel)
    }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "󱚣"
    tooltipText: root.lastError !== ""
      ? root.lastError
      : root.latestDay
        ? "AI usage · " + root.snapshot.accounts.length + (root.snapshot.accounts.length === 1 ? " account · " : " accounts · ") + root.displayText + " total on " + Model.dayLabel(root.latestDay.startDate)
        : "Loading AI usage"
    onPressed: function(b) {
      if (b === Qt.MiddleButton) root.refresh()
      else root.togglePanel()
    }
  }
}
