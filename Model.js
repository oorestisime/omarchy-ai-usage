.pragma library

function emptySnapshot() {
  return {
    schemaVersion: 1,
    accounts: [],
    combinedDailyUsage: [],
    summary: {},
    claude: null,
    openCodeGo: null,
    updatedAt: ""
  }
}

function parseSnapshot(text) {
  var value = JSON.parse(String(text || ""))
  if (!value || value.schemaVersion !== 1 || !(value.accounts instanceof Array))
    throw new Error("Collector returned an unsupported response")
  if (!(value.combinedDailyUsage instanceof Array)) value.combinedDailyUsage = []
  if (!value.summary || typeof value.summary !== "object") value.summary = {}
  return value
}

function number(value) {
  var parsed = Number(value)
  return isFinite(parsed) && parsed >= 0 ? parsed : 0
}

function formatTokens(value) {
  var count = number(value)
  if (count >= 1000000000) return (count / 1000000000).toFixed(count >= 10000000000 ? 1 : 2) + "B"
  if (count >= 1000000) return (count / 1000000).toFixed(count >= 100000000 ? 1 : 2) + "M"
  if (count >= 1000) return (count / 1000).toFixed(count >= 100000 ? 1 : 2) + "K"
  return String(Math.round(count))
}

function formatPercent(value) {
  var percent = number(value)
  if (percent <= 1) percent *= 100
  return Math.round(percent) + "%"
}

function clampPercent(value) {
  var percent = number(value)
  if (percent <= 1) percent *= 100
  return Math.max(0, Math.min(100, percent))
}

function accountName(account) {
  if (!account) return "ChatGPT"
  return String(account.email || account.label || "ChatGPT")
}

function accountId(account, index) {
  if (account) {
    var identity = account.id || account.email || account.label
    if (identity) return String(identity)
  }
  return "account-" + String(index === undefined ? 0 : index)
}

function accountMeta(account) {
  if (!account) return ""
  var lifetime = account.summary ? number(account.summary.lifetimeTokens) : 0
  return lifetime > 0 ? formatTokens(lifetime) + " lifetime" : ""
}

function titleCase(value) {
  var text = String(value || "")
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : ""
}

function limitSources(snapshot) {
  var result = []
  var accounts = snapshot && snapshot.accounts instanceof Array ? snapshot.accounts : []
  for (var i = 0; i < accounts.length; i++) {
    var account = accounts[i]
    result.push({
      id: accountId(account, i),
      name: accountName(account),
      meta: accountMeta(account),
      available: account.available === true,
      limits: account.limits instanceof Array ? account.limits : [],
      error: String(account.error || ""),
      resetCredits: number(account.resetCredits),
      dailyUsage: account.dailyUsage instanceof Array ? account.dailyUsage : [],
      dailyUsageAvailable: account.available === true,
      dailyUsageError: "",
      accountWide: true
    })
  }
  if (snapshot && snapshot.claude !== null && snapshot.claude !== undefined) {
    var claudeDays = snapshot.claude.dailyUsage instanceof Array ? snapshot.claude.dailyUsage : []
    result.push({
      id: "claude",
      name: "Claude",
      meta: titleCase(snapshot.claude.plan) + " · local · " + formatTokens(recentUsageTotal(claudeDays, snapshot, 7)) + " / 7d"
        + (snapshot.claude.cached ? " · cached" : ""),
      available: snapshot.claude.available === true,
      limits: snapshot.claude.limits instanceof Array ? snapshot.claude.limits : [],
      error: String(snapshot.claude.error || ""),
      resetCredits: 0,
      dailyUsage: claudeDays,
      dailyUsageAvailable: snapshot.claude.dailyUsageAvailable === true,
      dailyUsageError: String(snapshot.claude.dailyUsageError || ""),
      accountWide: false
    })
  }
  if (snapshot && snapshot.openCodeGo !== null && snapshot.openCodeGo !== undefined) {
    var goDays = snapshot.openCodeGo.dailyUsage instanceof Array ? snapshot.openCodeGo.dailyUsage : []
    result.push({
      id: "opencode-go",
      name: "OpenCode Go",
      meta: "Local · " + formatTokens(recentUsageTotal(goDays, snapshot, 7)) + " / 7d",
      available: snapshot.openCodeGo.available === true,
      limits: snapshot.openCodeGo.limits instanceof Array ? snapshot.openCodeGo.limits : [],
      error: String(snapshot.openCodeGo.error || ""),
      resetCredits: 0,
      dailyUsage: goDays,
      dailyUsageAvailable: snapshot.openCodeGo.dailyUsageAvailable === true,
      dailyUsageError: String(snapshot.openCodeGo.dailyUsageError || ""),
      accountWide: false
    })
  }
  return result
}

function chatgptSources(snapshot) {
  var count = snapshot && snapshot.accounts instanceof Array ? snapshot.accounts.length : 0
  return limitSources(snapshot).slice(0, count)
}

function sourceById(snapshot, identity) {
  var sources = limitSources(snapshot)
  for (var i = 0; i < sources.length; i++) {
    if (String(sources[i].id) === String(identity)) return sources[i]
  }
  return null
}

function shortAccountName(account) {
  var name = accountName(account)
  var at = name.indexOf("@")
  return at > 0 ? name.slice(0, at) : name
}

function tokensForDay(account, isoDate) {
  var days = account && account.dailyUsage instanceof Array ? account.dailyUsage : []
  for (var i = 0; i < days.length; i++) {
    if (String(days[i].startDate) === String(isoDate)) return number(days[i].tokens)
  }
  return null
}

function latestUsageDate(source) {
  var days = source && source.dailyUsage instanceof Array ? source.dailyUsage : []
  var latest = ""
  for (var i = 0; i < days.length; i++) {
    var value = String(days[i].startDate || "")
    if (value > latest) latest = value
  }
  return latest
}

function formatOptionalTokens(value) {
  return value === null || value === undefined ? "—" : formatTokens(value)
}

function recentDays(snapshot) {
  var days = snapshot && snapshot.combinedDailyUsage instanceof Array
    ? snapshot.combinedDailyUsage.slice(0)
    : []
  days.sort(function(a, b) { return String(a.startDate).localeCompare(String(b.startDate)) })
  return days.slice(Math.max(0, days.length - 7))
}

function parseDay(isoDate) {
  var parts = String(isoDate || "").split("-")
  if (parts.length !== 3) return null
  var date = new Date(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2]))
  return isFinite(date.getTime()) ? date : null
}

function dateKey(date) {
  if (!date) return ""
  return date.getFullYear() + "-"
    + String(date.getMonth() + 1).padStart(2, "0") + "-"
    + String(date.getDate()).padStart(2, "0")
}

function snapshotDay(snapshot) {
  var timestamp = Date.parse(String(snapshot && snapshot.updatedAt || ""))
  return isFinite(timestamp) ? new Date(timestamp) : new Date()
}

function recentUsageTotal(days, snapshot, count) {
  var end = snapshotDay(snapshot)
  var total = 0
  for (var offset = 0; offset < Math.max(1, count); offset++) {
    var date = new Date(end.getFullYear(), end.getMonth(), end.getDate() - offset)
    var value = tokensForDay({ dailyUsage: days }, dateKey(date))
    total += value === null ? 0 : number(value)
  }
  return total
}

function dailyRowsForSources(snapshot, sources, count) {
  if (!(sources instanceof Array) || !sources.length) return []
  var end = snapshotDay(snapshot)
  var rows = []
  for (var offset = Math.max(1, count) - 1; offset >= 0; offset--) {
    var date = new Date(end.getFullYear(), end.getMonth(), end.getDate() - offset)
    var key = dateKey(date)
    var segments = []
    var total = 0
    var incomplete = false
    for (var i = 0; i < sources.length; i++) {
      var value = tokensForDay(sources[i], key)
      if (sources[i].accountWide && value === null && key > latestUsageDate(sources[i])) incomplete = true
      var tokens = value === null ? 0 : number(value)
      segments.push({ accountId: String(sources[i].id), tokens: tokens })
      total += tokens
    }
    rows.push({ startDate: key, endDate: key, tokens: total, segments: segments, incomplete: incomplete })
  }
  return rows
}

function dailyRows(snapshot, count) {
  return dailyRowsForSources(snapshot, chatgptSources(snapshot), count)
}

function weeklyRowsForSources(snapshot, sources, count) {
  var weeks = Math.max(1, count)
  var days = dailyRowsForSources(snapshot, sources, weeks * 7)
  if (!days.length) return []
  var rows = []
  for (var week = 0; week < weeks; week++) {
    var first = days[week * 7]
    var last = days[week * 7 + 6]
    var segmentTotals = {}
    var segmentOrder = []
    var total = 0
    var incomplete = false
    for (var day = week * 7; day < week * 7 + 7; day++) {
      total += number(days[day].tokens)
      if (days[day].incomplete) incomplete = true
      for (var segment = 0; segment < days[day].segments.length; segment++) {
        var entry = days[day].segments[segment]
        var identity = String(entry.accountId)
        if (segmentTotals[identity] === undefined) {
          segmentTotals[identity] = 0
          segmentOrder.push(identity)
        }
        segmentTotals[identity] += number(entry.tokens)
      }
    }
    var segments = []
    for (var account = 0; account < segmentOrder.length; account++) {
      var accountIdentity = segmentOrder[account]
      segments.push({ accountId: accountIdentity, tokens: segmentTotals[accountIdentity] })
    }
    rows.push({
      startDate: first.startDate,
      endDate: last.endDate,
      tokens: total,
      segments: segments,
      incomplete: incomplete
    })
  }
  return rows
}

function weeklyRows(snapshot, count) {
  return weeklyRowsForSources(snapshot, chatgptSources(snapshot), count)
}

function usageViews(snapshot) {
  var result = []
  var all = limitSources(snapshot)
  var histories = []
  for (var sourceIndex = 0; sourceIndex < all.length; sourceIndex++) {
    if (all[sourceIndex].dailyUsageAvailable) histories.push(all[sourceIndex])
  }
  if (histories.length) {
    result.push({
      id: "all-days",
      title: "ALL SOURCES · 7 DAYS",
      rows: dailyRowsForSources(snapshot, histories, 7),
      rangeLabels: false,
      error: ""
    })
  }
  if (histories.length) {
    result.push({
      id: "all-weeks",
      title: "ALL SOURCES · 7 WEEKS",
      rows: weeklyRowsForSources(snapshot, histories, 7),
      rangeLabels: true,
      error: ""
    })
  }
  return result
}

function peakTokens(rows) {
  var peak = 0
  for (var i = 0; i < rows.length; i++) peak = Math.max(peak, number(rows[i].tokens))
  return peak
}

function totalTokens(rows) {
  var total = 0
  for (var i = 0; i < rows.length; i++) total += number(rows[i].tokens)
  return total
}

function hasIncomplete(rows) {
  for (var i = 0; i < rows.length; i++) {
    if (rows[i].incomplete) return true
  }
  return false
}

function compactDayLabel(isoDate) {
  var date = parseDay(isoDate)
  return date ? Qt.formatDate(date, "ddd d") : String(isoDate || "")
}

function weekRangeLabel(startDate, endDate) {
  var start = parseDay(startDate)
  var end = parseDay(endDate)
  if (!start || !end) return ""
  if (start.getMonth() === end.getMonth())
    return start.getDate() + "–" + end.getDate() + " " + Qt.formatDate(end, "MMM")
  return start.getDate() + " " + Qt.formatDate(start, "MMM") + "–" + end.getDate() + " " + Qt.formatDate(end, "MMM")
}

function latestDay(snapshot) {
  var days = recentDays(snapshot)
  return days.length ? days[days.length - 1] : null
}

function dayLabel(isoDate) {
  var parts = String(isoDate || "").split("-")
  if (parts.length !== 3) return String(isoDate || "")
  var date = new Date(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2]))
  return Qt.formatDate(date, "ddd d MMM")
}

function resetText(value, nowMs) {
  if (!value) return "Reset unknown"
  var when = Date.parse(String(value))
  if (!isFinite(when)) return "Reset unknown"
  var seconds = Math.max(0, Math.floor((when - (nowMs || Date.now())) / 1000))
  var days = Math.floor(seconds / 86400)
  var hours = Math.floor((seconds % 86400) / 3600)
  var minutes = Math.floor((seconds % 3600) / 60)
  if (days > 0) return "Resets in " + days + "d " + hours + "h"
  if (hours > 0) return "Resets in " + hours + "h " + minutes + "m"
  return "Resets in " + minutes + "m"
}

function updatedText(value, nowMs) {
  var when = Date.parse(String(value || ""))
  if (!isFinite(when)) return "Not updated yet"
  var seconds = Math.max(0, Math.floor(((nowMs || Date.now()) - when) / 1000))
  if (seconds < 60) return "Updated just now"
  if (seconds < 3600) return "Updated " + Math.floor(seconds / 60) + "m ago"
  return "Updated " + Math.floor(seconds / 3600) + "h ago"
}
