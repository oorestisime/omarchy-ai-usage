import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

Panel {
  id: root
  moduleName: "oorestisime.ai-usage"
  ipcTarget: moduleName
  manageIpc: false

  property var anchorItem: null
  property var hostWidget: null
  property var usage: null
  property double now: Date.now()

  readonly property var barIdentity: hostWidget || root
  readonly property var snapshot: usage ? usage.snapshot : Model.emptySnapshot()
  readonly property var weekRows: Model.dailyRows(snapshot, 7)
  readonly property var sevenWeekRows: Model.weeklyRows(snapshot, 7)
  readonly property real weekPeak: Model.peakTokens(weekRows)
  readonly property real sevenWeekPeak: Model.peakTokens(sevenWeekRows)
  readonly property int accountCount: snapshot.accounts.length
  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color dim: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.62)
  readonly property color faint: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.10)
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property string stateHome: Quickshell.env("XDG_STATE_HOME") || Quickshell.env("HOME") + "/.local/state"
  property var themeSeriesColors: []
  readonly property var accountColors: buildAccountColors(snapshot.accounts)

  function generatedSeriesColor(index) {
    var hue = Color.accent.hslHue
    if (!isFinite(hue) || hue < 0) hue = 0.62
    return Qt.hsla(
      (hue + 0.14 + index * 0.19) % 1,
      Math.max(0.55, Color.accent.hslSaturation),
      Math.max(0.55, Math.min(0.72, Color.accent.hslLightness)),
      1
    )
  }

  function loadThemePalette(raw) {
    var source = String(raw || "")
    var names = ["magenta", "cyan", "green", "yellow", "orange"]
    var colors = []
    for (var i = 0; i < names.length; i++) {
      var pattern = new RegExp("^\\s*" + names[i] + "\\s*=\\s*[\\\"']?(#[0-9A-Fa-f]{6})", "m")
      var match = source.match(pattern)
      if (match) colors.push(match[1])
    }
    themeSeriesColors = colors
  }

  function buildAccountColors(accounts) {
    var result = {}
    var rows = accounts instanceof Array ? accounts : []
    for (var i = 0; i < rows.length; i++) {
      var identity = Model.accountId(rows[i], i)
      result[identity] = i === 0
        ? Color.accent
        : (themeSeriesColors[i - 1] || generatedSeriesColor(i - 1))
    }
    return result
  }

  function accountColor(identity) {
    return accountColors[String(identity)] || Color.accent
  }

  FileView {
    id: themePalette
    path: root.stateHome + "/omarchy/current/theme/colors.toml"
    watchChanges: true
    printErrors: false
    onLoaded: root.loadThemePalette(text())
    onFileChanged: reload()
    onLoadFailed: root.themeSeriesColors = []
  }

  function open() {
    if (usage) usage.refresh()
    root.controller.show()
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }

  function close() { root.controller.hide() }
  function toggle() { if (root.opened) close(); else open() }
  function switchPanel(direction) {
    if (bar && typeof bar.switchPanelFrom === "function") return bar.switchPanelFrom(barIdentity, direction)
    return false
  }

  Timer {
    interval: 60000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.now = Date.now()
  }

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.barIdentity
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(680))
    contentHeight: panel.fittedContentHeight(content.implicitHeight, Style.space(650))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onActivateRequested: if (root.usage) root.usage.refresh()
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(text) {
        if ((text === "r" || text === "R") && root.usage) root.usage.refresh()
      }

      Flickable {
        anchors.fill: parent
        contentWidth: width
        contentHeight: content.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds

        Column {
          id: content
          width: parent.width
          spacing: Style.space(14)

          Item {
            width: parent.width
            implicitHeight: Math.max(hero.implicitHeight, refreshButton.implicitHeight)

            PanelHero {
              id: hero
              anchors.left: parent.left
              anchors.right: refreshButton.left
              anchors.rightMargin: Style.space(10)
              anchors.verticalCenter: parent.verticalCenter
              title: "AI usage"
              meta: root.accountCount + (root.accountCount === 1 ? " account" : " accounts")
              detail: ""
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            PanelActionButton {
              id: refreshButton
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              iconText: "R"
              tooltipText: "Refresh usage"
              foreground: root.foreground
              fontFamily: root.fontFamily
              enabled: root.usage && !root.usage.refreshing
              onClicked: root.usage.refresh()
            }
          }

          Text {
            visible: root.usage && root.usage.lastError !== ""
            width: parent.width
            text: root.usage ? root.usage.lastError : ""
            textFormat: Text.PlainText
            color: root.urgent
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
          }

          SectionLabel { text: "ACCOUNT LIMITS" }

          Grid {
            id: accountGrid
            width: parent.width
            columns: Math.min(2, Math.max(1, root.accountCount))
            columnSpacing: Style.space(10)
            rowSpacing: Style.space(10)

            Repeater {
              model: root.snapshot.accounts

              BorderSurface {
                required property var modelData
                required property int index
                width: (accountGrid.width - accountGrid.columnSpacing * (accountGrid.columns - 1)) / accountGrid.columns
                implicitHeight: accountCard.implicitHeight + Style.space(24)
                color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.035)
                radius: Style.cornerRadius
                borderSpec: Border.flat(root.faint, 1)

                Column {
                  id: accountCard
                  readonly property string accountIdentity: Model.accountId(parent.modelData, parent.index)
                  readonly property color seriesColor: root.accountColor(accountIdentity)
                  anchors.left: parent.left
                  anchors.right: parent.right
                  anchors.verticalCenter: parent.verticalCenter
                  anchors.leftMargin: Style.space(12)
                  anchors.rightMargin: Style.space(12)
                  spacing: Style.space(6)

                  Item {
                    id: accountHeader
                    readonly property var account: accountCard.parent.modelData
                    width: parent.width
                    implicitHeight: Math.max(accountName.implicitHeight, accountMeta.implicitHeight)

                    Text {
                      id: accountName
                      anchors.left: parent.left
                      anchors.right: accountMeta.left
                      anchors.rightMargin: Style.space(7)
                      anchors.verticalCenter: parent.verticalCenter
                      text: Model.accountName(accountHeader.account)
                      textFormat: Text.PlainText
                      color: accountCard.seriesColor
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.bodySmall
                      font.bold: true
                      elide: Text.ElideRight
                    }

                    Text {
                      id: accountMeta
                      anchors.right: parent.right
                      anchors.verticalCenter: parent.verticalCenter
                      text: Model.accountMeta(accountHeader.account)
                      textFormat: Text.PlainText
                      color: root.dim
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.caption
                    }
                  }

                  Text {
                    visible: !parent.parent.modelData.available
                    width: parent.width
                    text: parent.parent.modelData.error || "Usage unavailable"
                    textFormat: Text.PlainText
                    color: root.urgent
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.bodySmall
                    wrapMode: Text.WordWrap
                  }

                  Repeater {
                    model: parent.parent.modelData.limits || []
                    LimitRow {
                      required property var modelData
                      width: accountCard.width
                      label: modelData.label
                      percent: modelData.percent
                      resetsAt: modelData.resetsAt
                      accentColor: accountCard.seriesColor
                    }
                  }

                  Text {
                    visible: parent.parent.modelData.resetCredits > 0
                    width: parent.width
                    text: parent.parent.modelData.resetCredits + (parent.parent.modelData.resetCredits === 1 ? " free reset" : " free resets")
                    textFormat: Text.PlainText
                    color: root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                  }
                }
              }
            }
          }

          SectionLabel { text: "TOKEN USAGE" }

          Text {
            visible: root.weekRows.length === 0
            width: parent.width
            text: "No daily usage buckets were returned."
            textFormat: Text.PlainText
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
          }

          Row {
            id: usageCards
            visible: root.weekRows.length > 0
            width: parent.width
            spacing: Style.space(10)

            UsageCard {
              width: (usageCards.width - usageCards.spacing) / 2
              title: "THIS WEEK"
              rows: root.weekRows
              peak: root.weekPeak
              rangeLabels: false
              rowHeight: Style.space(20)
            }

            UsageCard {
              width: (usageCards.width - usageCards.spacing) / 2
              title: "LAST 7 WEEKS"
              rows: root.sevenWeekRows
              peak: root.sevenWeekPeak
              rangeLabels: true
              rowHeight: Style.space(20)
            }
          }

          Column {
            visible: root.snapshot.openCodeGo !== null
            width: parent.width
            spacing: Style.space(8)

            SectionLabel { text: "OPENCODE GO LIMITS" }

            Text {
              visible: root.snapshot.openCodeGo && !root.snapshot.openCodeGo.available
              width: parent.width
              text: root.snapshot.openCodeGo ? root.snapshot.openCodeGo.error : ""
              textFormat: Text.PlainText
              color: root.urgent
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
              wrapMode: Text.WordWrap
            }

            Grid {
              id: goGrid
              width: parent.width
              columns: Math.min(3, Math.max(1, root.snapshot.openCodeGo ? root.snapshot.openCodeGo.limits.length : 0))
              columnSpacing: Style.space(10)
              rowSpacing: Style.space(10)

              Repeater {
                model: root.snapshot.openCodeGo ? root.snapshot.openCodeGo.limits : []
                BorderSurface {
                  required property var modelData
                  required property int index
                  width: (goGrid.width - goGrid.columnSpacing * (goGrid.columns - 1)) / goGrid.columns
                  implicitHeight: goLimit.implicitHeight + Style.space(20)
                  color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.035)
                  radius: Style.cornerRadius
                  borderSpec: Border.flat(root.faint, 1)

                  LimitRow {
                    id: goLimit
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.verticalCenter: parent.verticalCenter
                    anchors.leftMargin: Style.space(10)
                    anchors.rightMargin: Style.space(10)
                    label: parent.modelData.label
                    percent: parent.modelData.percent
                    resetsAt: parent.modelData.resetsAt
                  }
                }
              }
            }
          }

        }
      }
    }
  }

  component SectionLabel: Text {
    color: root.dim
    font.family: root.fontFamily
    font.pixelSize: Style.font.caption
    font.bold: true
    font.letterSpacing: 1.1
    textFormat: Text.PlainText
  }

  component UsageCard: BorderSurface {
    id: usageCard
    property string title: "USAGE"
    property var rows: []
    property real peak: 0
    property bool rangeLabels: false
    property real rowHeight: Style.space(20)

    implicitHeight: Style.space(190)
    color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.035)
    radius: Style.cornerRadius
    borderSpec: Border.flat(root.faint, 1)

    Column {
      anchors.fill: parent
      anchors.margins: Style.space(11)
      spacing: Style.space(3)

      Item {
        width: parent.width
        implicitHeight: Math.max(cardTitle.implicitHeight, cardTotal.implicitHeight)

        Text {
          id: cardTitle
          anchors.left: parent.left
          anchors.verticalCenter: parent.verticalCenter
          text: usageCard.title
          textFormat: Text.PlainText
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          font.bold: true
          font.letterSpacing: 0.8
        }

        Text {
          id: cardTotal
          anchors.right: parent.right
          anchors.verticalCenter: parent.verticalCenter
          text: Model.formatTokens(Model.totalTokens(usageCard.rows)) + " total"
          textFormat: Text.PlainText
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          font.bold: true
        }
      }

      Repeater {
        model: usageCard.rows
        UsageRow {
          required property var modelData
          required property int index
          width: usageCard.width - Style.space(22)
          implicitHeight: usageCard.rowHeight
          row: modelData
          peak: usageCard.peak
          rangeLabel: usageCard.rangeLabels
        }
      }
    }
  }

  component UsageRow: Item {
    id: usageRow
    property var row: null
    property real peak: 0
    property bool rangeLabel: false

    Text {
      id: rowLabel
      anchors.left: parent.left
      anchors.verticalCenter: parent.verticalCenter
      width: Style.space(78)
      text: usageRow.rangeLabel
        ? Model.weekRangeLabel(usageRow.row.startDate, usageRow.row.endDate)
        : Model.compactDayLabel(usageRow.row.startDate)
      textFormat: Text.PlainText
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      elide: Text.ElideRight
    }

    Rectangle {
      id: usageTrack
      anchors.left: rowLabel.right
      anchors.leftMargin: Style.space(7)
      anchors.right: rowValue.left
      anchors.rightMargin: Style.space(8)
      anchors.verticalCenter: parent.verticalCenter
      height: Style.space(5)
      radius: height / 2
      color: root.faint
      clip: true

      Row {
        id: usageStack
        width: parent.width * (usageRow.peak > 0 ? Number(usageRow.row.tokens || 0) / usageRow.peak : 0)
        height: parent.height

        Repeater {
          model: usageRow.row && usageRow.row.segments ? usageRow.row.segments : []
          Rectangle {
            required property var modelData
            width: usageRow.row.tokens > 0 ? usageStack.width * Number(modelData.tokens || 0) / usageRow.row.tokens : 0
            height: usageStack.height
            color: root.accountColor(modelData.accountId)
          }
        }
      }
    }

    Text {
      id: rowValue
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      width: Style.space(57)
      text: Model.formatTokens(usageRow.row ? usageRow.row.tokens : 0)
      textFormat: Text.PlainText
      horizontalAlignment: Text.AlignRight
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      font.bold: true
    }
  }

  component LimitRow: Column {
    property string label: "Limit"
    property real percent: 0
    property string resetsAt: ""
    property color accentColor: Color.accent
    spacing: Style.space(3)

    Item {
      width: parent.width
      implicitHeight: Math.max(limitLabel.implicitHeight, limitValue.implicitHeight)

      Text {
        id: limitLabel
        anchors.left: parent.left
        text: parent.parent.label
        textFormat: Text.PlainText
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
      }

      Text {
        id: limitValue
        anchors.right: parent.right
        text: Model.formatPercent(parent.parent.percent)
        textFormat: Text.PlainText
        color: Model.clampPercent(parent.parent.percent) >= 90 ? root.urgent : root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
        font.bold: true
      }
    }

    Rectangle {
      width: parent.width
      height: Style.space(5)
      radius: height / 2
      color: root.faint

      Rectangle {
        width: parent.width * Model.clampPercent(parent.parent.percent) / 100
        height: parent.height
        radius: parent.radius
        color: Model.clampPercent(parent.parent.percent) >= 90 ? root.urgent : parent.parent.accentColor
      }
    }

    Text {
      text: Model.resetText(parent.resetsAt, root.now)
      textFormat: Text.PlainText
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
    }
  }
}
