// =====================================================================
// Glow Action Script - Remove Browser Extension (macOS, JXA)
//
// Removes a browser extension from Chrome (and other Chromium browsers,
// future) by extension id. Multiple layers, all run for max robustness:
//
//   Layer 1 (primary, enforced): Chrome Enterprise Policy via managed prefs.
//     Merge { ExtensionSettings: { <id>: { installation_mode: "removed" } } }
//     into BOTH managed-preferences paths (root, RTR has it):
//       a) System-level  /Library/Managed Preferences/<bundle>.plist
//          Chrome reads as Machine/Mandatory. On macOS <=13 (Ventura) it
//          persists and blocks the ext for ALL users indefinitely. On macOS
//          14+ (Sonoma) managedclient prunes this orphan at reboot, but it is
//          still enforced while present.
//       b) Per-user  /Library/Managed Preferences/<console-user>/<bundle>.plist
//          Chrome reads as Current-user/Mandatory and it SURVIVES the active
//          login session on Sonoma (pruned only at the next logout/login).
//          This is what makes RTR-only removal work on Sonoma without MDM.
//     After writing, flush cfprefsd, kill the browser, then RELAUNCH it inside
//     the console user's GUI session so Chrome's policy engine reads the fresh
//     policy and force-uninstalls the extension immediately, before any prune.
//     This sidesteps the Secure Preferences HMAC that guards on-disk ext state.
//
//   Layer 2 (defense-in-depth): filesystem removal of residual extension
//     files + data across all profiles and all users. This is what makes a
//     "removed" extension STAY gone even after the orphan managed-prefs file
//     is later pruned at a session boundary.
//
//
// RTR contract:
//   - Input  : single base64-encoded JSON passed as $args[0].
//   - Output : one compact JSON object on stdout (the ActionResult envelope).
//   - Stderr : SILENT. All errors are reported inside the JSON envelope.
//   - Diag   : file-only at /tmp/glow/rtr.txt. Never stderr.
//
// Local testing (no args uses safe defaults: dry_run=true):
//   INPUT=$(printf '%s' '{"params":{"extension_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},"dry_run":true}' | base64)
//   sudo osascript -l JavaScript remove-browser-extension.js "$INPUT"
// =====================================================================

ObjC.import('Foundation');
ObjC.import('stdlib'); // for $.kill (SIGKILL escalation in _runCommand)

// ===== Section 0: Constants =====

var SCRIPT_VERSION = 4;
var OS_FAMILY      = 'mac';
var DIAG_FILE      = '/tmp/glow/rtr.txt';

var DEFAULT_CMD_TIMEOUT_SEC  = 20;
var SIGKILL_GRACE_SEC        = 2;
var BROWSER_EXIT_WAIT_SEC    = 3;   // max seconds to wait for browser to exit after kill
var BROWSER_EXIT_POLL_SEC    = 0.5; // polling interval while waiting

// Chrome store ids are exactly 32 chars in [a-p]. Enforce strictly: the id is
// used as a plist dictionary key and as a path leaf - strict validation blocks
// any injection or traversal via a malformed id. Relax when adding non-Chrome
// browsers if their id scheme differs.
var EXT_ID_RE = /^[a-p]{32}$/;

// Chromium family browser table.
// CURRENT SCOPE: only `chrome` is supported. Passing any other browser key
// returns UNSUPPORTED_BROWSER. Edge, Brave, and Chromium use the same policy
// engine and can be added as table rows with no logic changes. Opera and
// Vivaldi have limited/no ExtensionSettings support - FS layer only.
// Ref: https://chromeenterprise.google/policies/#ExtensionSettings
// Ref: https://support.google.com/chrome/a/answer/9020077 (managed prefs on macOS)
var BROWSERS = {
  chrome: {
    bundleId:     'com.google.Chrome',
    managedPrefs: '/Library/Managed Preferences/com.google.Chrome.plist',
    userDataRel:  'Library/Application Support/Google/Chrome', // relative to each home dir
    processName:  'Google Chrome', // pkill -x exact match
  },
};

// Profile dir names inside a Chromium User Data dir that hold per-profile data.
var PROFILE_NAME_RE = /^(Default|Profile \d+)$/;

// Per-profile subdirs that contain extension-scoped data keyed by extension id.
// Ref: https://chromium.googlesource.com/chromium/src/+/main/chrome/browser/extensions/
var PROFILE_EXT_DATA_SUBDIRS = [
  'Extensions',
  'Local Extension Settings',
  'Sync Extension Settings',
];

var _fm = $.NSFileManager.defaultManager;

// ===== Section 1: Small Helpers =====

function nowIso() {
  var f = $.NSISO8601DateFormatter.alloc.init;
  return f.stringFromDate($.NSDate.date).js;
}

function uuidString() {
  return $.NSUUID.UUID.UUIDString.js;
}

function _nilToNull(v) {
  return v && v.isNil && v.isNil() ? null : v;
}

function writeDiag(msg) {
  try {
    var dir = '/tmp/glow';
    if (!isDirectory(dir)) {
      _fm.createDirectoryAtPathWithIntermediateDirectoriesAttributesError(dir, true, $(), null);
    }
    var line = '[' + nowIso() + '] ' + msg + '\n';
    var data = $.NSString.alloc.initWithUTF8String(line).dataUsingEncoding($.NSUTF8StringEncoding);
    var fh = $.NSFileHandle.fileHandleForWritingAtPath(DIAG_FILE);
    if (fh.isNil()) {
      // First write: file does not exist yet - create it.
      $.NSString.alloc.initWithUTF8String(line)
        .writeToFileAtomicallyEncodingError(DIAG_FILE, true, $.NSUTF8StringEncoding, null);
    } else {
      fh.seekToEndOfFile;
      fh.writeData(data);
      fh.closeFile;
    }
  } catch (e) { /* never throw from diagnostics */ }
}

function hostname() {
  try { return $.NSProcessInfo.processInfo.hostName.js || ''; } catch (e) { return ''; }
}

// Serial number via ioreg (IOPlatformSerialNumber).
function serialNumber() {
  try {
    var r = _runCommand('/usr/sbin/ioreg', ['-c', 'IOPlatformExpertDevice', '-d', '2']);
    if (r.exitCode === 0 && r.stdout) {
      var m = r.stdout.match(/"IOPlatformSerialNumber"\s*=\s*"([^"]+)"/);
      if (m) return m[1];
    }
  } catch (e) {}
  return '';
}

// macOS major version (14 = Sonoma, 13 = Ventura, ...). Telemetry only.
// Enforcement does NOT branch on this - managed prefs are honored on every version.
function getOSMajorVersion() {
  try {
    var r = _runCommand('/usr/bin/sw_vers', ['-productVersion']);
    if (r.exitCode === 0 && r.stdout) {
      var major = parseInt(r.stdout.trim().split('.')[0], 10);
      if (!isNaN(major)) return major;
    }
  } catch (e) {}
  writeDiag('WARN: could not determine macOS version');
  return 0;
}

// Major version of the installed browser app (e.g. 126 for Chrome 126.x).
// Telemetry only. Chrome reads /Library/Managed Preferences/ as Mandatory policy
// regardless of build (the prior "119+ ignores it" was a pruned-orphan bug), so
// enforcement does not branch on this - it is recorded in the envelope for diag.
// Reads CFBundleShortVersionString from the app bundle. Returns int or null.
function getBrowserMajorVersion(browser) {
  try {
    var infoPlist = '/Applications/' + browser.processName + '.app/Contents/Info';
    var r = _runCommand('/usr/bin/defaults', ['read', infoPlist, 'CFBundleShortVersionString']);
    if (r.exitCode === 0 && r.stdout) {
      var major = parseInt(r.stdout.trim().split('.')[0], 10);
      if (!isNaN(major)) return major;
    }
  } catch (e) {}
  return null; // app not in /Applications or version unreadable - caller falls back to osMajor
}

// ===== Section 2: Filesystem Helpers =====

function fileExists(path) {
  return _fm.fileExistsAtPath(path);
}

function isDirectory(path) {
  var isDir = Ref();
  var exists = _fm.fileExistsAtPathIsDirectory(path, isDir);
  return exists && isDir[0];
}

function _isSymlink(path) {
  try {
    var err   = Ref();
    var attrs = _nilToNull(_fm.attributesOfItemAtPathError(path, err));
    if (!attrs) return false;
    var ftype = _nilToNull(attrs.objectForKey($.NSFileType));
    return ftype ? ftype.isEqualToString($.NSFileTypeSymbolicLink) : false;
  } catch (e) { return false; }
}

function listDirectory(path) {
  if (!isDirectory(path)) return [];
  var err   = Ref();
  var items = _nilToNull(_fm.contentsOfDirectoryAtPathError(path, err));
  if (!items) return [];
  var out = [];
  for (var i = 0; i < items.count; i++) out.push(items.objectAtIndex(i).js);
  return out;
}

function removeItem(path) {
  if (!fileExists(path)) return true;
  var err = Ref();
  return _fm.removeItemAtPathError(path, err);
}

// Resolve symlink chain; reject control chars and user-planted symlinks.
// Returns canonical path or null. macOS maps /tmp,/var,/etc to /private/*;
// strip that prefix so only real symlinks cause a mismatch.
function resolveSafe(path) {
  if (!path || typeof path !== 'string') return null;
  if (/[\x00-\x1f]/.test(path)) return null;
  if (path.indexOf('..') !== -1) return null;
  var resolved, standardized;
  try {
    resolved     = $(path).stringByResolvingSymlinksInPath.js;
    standardized = $(path).stringByStandardizingPath.js;
  } catch (e) { return null; }
  if (!resolved || !standardized) return null;
  var sys = ['/tmp', '/var', '/etc'];
  for (var i = 0; i < sys.length; i++) {
    if (resolved === sys[i] || resolved.indexOf(sys[i] + '/') === 0) {
      resolved = '/private' + resolved;
      break;
    }
  }
  var cmp = resolved;
  if (!/^\/private\//.test(standardized) && /^\/private\//.test(cmp)) {
    cmp = cmp.replace(/^\/private/, '');
  }
  if (standardized !== cmp) return null;
  if (_isSymlink(resolved)) return null;
  return resolved;
}

// ===== Section 3: Plist Read / Write =====

function _nsToJs(nsObj) {
  var err      = Ref();
  var jsonData = _nilToNull($.NSJSONSerialization.dataWithJSONObjectOptionsError(nsObj, 0, err));
  if (!jsonData) return null;
  var jsonStr = _nilToNull($.NSString.alloc.initWithDataEncoding(jsonData, $.NSUTF8StringEncoding));
  if (!jsonStr) return null;
  try { return JSON.parse(jsonStr.js); } catch (e) { return null; }
}

// Read a plist as a JS object. Returns null if file missing.
// Returns the sentinel { _parse_failed: true } if the file exists but cannot
// be parsed - callers must treat this as an error, not as an absent file.
function readPlistDict(path) {
  if (!fileExists(path)) return null;
  try {
    var dict = $.NSDictionary.dictionaryWithContentsOfFile(path);
    if (dict && !dict.isNil()) {
      var js = _nsToJs(dict);
      if (js !== null) return js;
      // File exists and is a plist but _nsToJs failed (e.g. non-JSON-serializable
      // types from an MDM-written policy). Return sentinel rather than null so
      // callers do not mistake this for "file absent".
      return { _parse_failed: true };
    }
  } catch (e) {}
  return { _parse_failed: true }; // file exists but could not be read at all
}

// Write a JS object as a plist atomically. Guards against writing through a
// symlinked target file. JXA bridges plain JS objects to NSDictionary via $().
// Returns { ok: bool, error: string|null }.
function writePlistDict(path, obj) {
  if (_isSymlink(path)) return { ok: false, error: 'target is a symlink' };
  try {
    var nsDict = $(obj);
    var wrote  = nsDict.writeToFileAtomically(path, true) === true;
    return { ok: wrote, error: wrote ? null : 'writeToFileAtomically returned false: ' + path };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

// ===== Section 4: NSTask Helper =====

// Run a command with a timeout. SIGTERM after DEFAULT_CMD_TIMEOUT_SEC; SIGKILL
// after an additional SIGKILL_GRACE_SEC if the process does not exit.
function _runCommand(launchPath, args) {
  var outFile, errFile, outFh, errFh;
  try {
    var dir = '/tmp/glow';
    if (!isDirectory(dir)) {
      _fm.createDirectoryAtPathWithIntermediateDirectoriesAttributesError(dir, true, $(), null);
    }
    var uid = uuidString();
    outFile = dir + '/cmd_out_' + uid;
    errFile = dir + '/cmd_err_' + uid;
    _fm.createFileAtPathContentsAttributes(outFile, $(), $());
    _fm.createFileAtPathContentsAttributes(errFile, $(), $());

    var task = $.NSTask.alloc.init;
    task.launchPath = launchPath;
    if (args) task.arguments = args;
    outFh = $.NSFileHandle.fileHandleForWritingAtPath(outFile);
    errFh = $.NSFileHandle.fileHandleForWritingAtPath(errFile);
    task.standardOutput = outFh;
    task.standardError  = errFh;
    task.launch;

    var pid = task.processIdentifier;
    var killTimer = $.NSTimer.scheduledTimerWithTimeIntervalRepeatsBlock(
      DEFAULT_CMD_TIMEOUT_SEC, false, function () {
        try { task.terminate; } catch (_) {}
      }
    );
    var hardKill = $.NSTimer.scheduledTimerWithTimeIntervalRepeatsBlock(
      DEFAULT_CMD_TIMEOUT_SEC + SIGKILL_GRACE_SEC, false, function () {
        try { $.kill(pid, 9); } catch (_) {}
      }
    );
    task.waitUntilExit;
    if (killTimer.isValid) killTimer.invalidate;
    if (hardKill.isValid)  hardKill.invalidate;

    outFh.closeFile;
    errFh.closeFile;
    var out    = _readSmallFile(outFile);
    var errStr = _readSmallFile(errFile);
    removeItem(outFile);
    removeItem(errFile);
    return { exitCode: task.terminationStatus, stdout: out, stderr: errStr };
  } catch (e) {
    if (outFile) removeItem(outFile);
    if (errFile) removeItem(errFile);
    return { exitCode: -1, stdout: '', stderr: String(e) };
  }
}

function _readSmallFile(path) {
  try {
    var s = $.NSString.stringWithContentsOfFileEncodingError(path, $.NSUTF8StringEncoding, null);
    return s && !s.isNil() ? s.js : '';
  } catch (e) { return ''; }
}

// Poll until the process is gone or maxSec elapsed. Returns true if exited.
function _waitForExit(processName, maxSec) {
  var waited = 0;
  while (waited < maxSec) {
    var r = _runCommand('/usr/bin/pgrep', ['-x', processName]);
    if (r.exitCode !== 0) return true; // no matching process
    $.NSThread.sleepForTimeInterval(BROWSER_EXIT_POLL_SEC);
    waited += BROWSER_EXIT_POLL_SEC;
  }
  return false; // timed out
}

// Flush cfprefsd so Chrome reads the freshly-written managed prefs on its next
// launch. Writing directly to /Library/Managed Preferences/ bypasses cfprefsd's
// in-memory cache. If we kill Chrome before cfprefsd propagates the new plist,
// Chrome reads the stale (empty) cache and the policy is not applied, leaving
// the extension in a "corrupted" state that Chrome then re-downloads.
// cfprefsd is a launchd-managed daemon - it auto-restarts within milliseconds.
// We poll until it is back (or maxSec elapses) before returning, so the caller
// can be confident cfprefsd is serving fresh data before Chrome is killed.
// Must run AFTER policy write and BEFORE Chrome kill.
// Only call when a new policy was actually written (not on already_present runs).
// Returns { flushed: bool, cfprefsd_back: bool }.
function flushPolicyCache(dryRun) {
  if (dryRun) return { flushed: false, dry_run: true };
  var r = _runCommand('/usr/bin/killall', ['cfprefsd']);
  var flushed = r.exitCode === 0 || r.exitCode === 1; // 1 = already gone, still ok
  // Poll until cfprefsd is back up (launchd restarts it; typically <100ms).
  var back = false;
  var maxSec = 3, waited = 0, poll = 0.1;
  while (waited < maxSec) {
    $.NSThread.sleepForTimeInterval(poll);
    waited += poll;
    var chk = _runCommand('/usr/bin/pgrep', ['cfprefsd']);
    if (chk.exitCode === 0) { back = true; break; }
  }
  if (!back) writeDiag('WARN: cfprefsd did not restart within ' + maxSec + 's');
  return { flushed: flushed, cfprefsd_back: back };
}

// Kill the browser so the policy engine re-reads the updated managed prefs on
// its next launch. Chrome does NOT auto-restart - the caller relaunches it in
// the console user's GUI session (see relaunchBrowser) so the fresh policy is
// read immediately, within the same session, before any managedclient prune.
// Returns { process, was_running, killed, exited_cleanly }.
function killBrowser(browser, dryRun) {
  var rec = { process: browser.processName, was_running: false, killed: false, exited_cleanly: false };
  if (dryRun) { rec.dry_run = true; return rec; }

  // Check if browser is running before attempting kill.
  var running = _runCommand('/usr/bin/pgrep', ['-x', browser.processName]);
  rec.was_running = running.exitCode === 0;
  if (!rec.was_running) {
    // Not running - policy will apply on next user launch. Nothing to kill.
    return rec;
  }

  // -x exact-name match kills the main browser process. The second call matches
  // helper processes (Renderer, GPU, etc.) by their full .app/ bundle path.
  var r1 = _runCommand('/usr/bin/pkill', ['-x', browser.processName]);
  var r2 = _runCommand('/usr/bin/pkill', ['-f', '/' + browser.processName + '.app/']);
  rec.killed = r1.exitCode === 0 || r2.exitCode === 0;

  // Wait for the process to fully exit before filesystem removal begins.
  // Skipping this risks Chrome helper processes still holding LevelDB locks
  // on extension data dirs, or Chrome recreating files after our delete.
  rec.exited_cleanly = _waitForExit(browser.processName, BROWSER_EXIT_WAIT_SEC);
  if (!rec.exited_cleanly) {
    writeDiag('WARN: ' + browser.processName + ' did not exit within ' + BROWSER_EXIT_WAIT_SEC + 's after kill');
  }
  return rec;
}

// Resolve the user owning the active GUI (console) session. RTR runs as root; we
// need the real logged-in user to (a) write their per-user managed-prefs file and
// (b) relaunch Chrome inside their session. Returns { user, uid } or null when no
// real user is logged in (console = root / loginwindow / _windowserver = login
// screen, fast-user-switch gap, or headless). System accounts (uid < 500) are
// rejected. When null, the per-user policy + relaunch are skipped (Chrome is not
// running in a user session anyway); system policy + FS layer still run.
function resolveConsoleUser() {
  try {
    var r = _runCommand('/usr/bin/stat', ['-f', '%Su', '/dev/console']);
    var user = (r.exitCode === 0 && r.stdout) ? r.stdout.trim() : '';
    if (!user || user === 'root' || user === 'loginwindow' || user === '_windowserver') return null;
    var idr = _runCommand('/usr/bin/id', ['-u', user]);
    var uid = (idr.exitCode === 0 && idr.stdout) ? parseInt(idr.stdout.trim(), 10) : NaN;
    if (isNaN(uid) || uid < 500) return null;
    return { user: user, uid: uid };
  } catch (e) { return null; }
}

// Relaunch the browser inside a user's GUI session from root via launchctl
// asuser. This forces Chrome's policy engine to read the freshly-written managed
// prefs immediately, in-session, before managedclient prunes the orphan file.
// Without this we would rely on the user reopening Chrome later - by which time a
// logout could have pruned the per-user policy.
// Returns { relaunched, uid, dry_run, error?, stderr? }.
function relaunchBrowser(browser, uid, dryRun) {
  var rec = { relaunched: false, uid: uid, dry_run: dryRun };
  if (dryRun) return rec;
  var r = _runCommand('/bin/launchctl', ['asuser', String(uid), '/usr/bin/open', '-a', browser.processName]);
  rec.relaunched = r.exitCode === 0;
  if (!rec.relaunched) {
    rec.error  = 'RELAUNCH_FAILED';
    rec.stderr = (r.stderr || r.stdout || '').trim();
    writeDiag('WARN: relaunch of ' + browser.processName + ' in session uid=' + uid + ' failed: ' + rec.stderr);
  }
  return rec;
}

// ===== Section 5: Layer 1 - Enterprise Policy =====

// ----- 5a: managed-preferences plist (all macOS) -----
// Chrome reads /Library/Managed Preferences/ as Mandatory policy on every macOS
// version (the prior "Chrome 119+ ignores it" belief was wrong - the bug was a
// pruned orphan file, not an ignored one). `path` selects the scope:
//   - System path  -> Machine/Mandatory, blocks all users (persists pre-Sonoma).
//   - Per-user path -> Current-user/Mandatory, survives the active login session.
// Both are written each run; see run() for the full strategy.
function applyRemovalPolicy(browser, extId, path, scope, dryRun) {
  var result = {
    layer:           'policy',
    scope:           scope,
    path:            path,
    written:         false,
    merged_existing: false,
    already_present: false,
    dry_run:         dryRun,
  };

  var existing = readPlistDict(path);

  // Guard: if the file exists but we cannot parse it, abort rather than
  // overwrite. Clobbering an unreadable MDM-managed policy is worse than
  // leaving the extension in place.
  if (existing && existing._parse_failed) {
    result.error = 'POLICY_READ_FAILED';
    writeDiag('ERROR: existing plist at ' + path + ' could not be parsed - aborting policy write to avoid data loss');
    return result;
  }

  var policy = (existing && typeof existing === 'object') ? existing : {};
  if (existing) result.merged_existing = true;

  var extSettings = (policy.ExtensionSettings && typeof policy.ExtensionSettings === 'object')
    ? policy.ExtensionSettings : {};

  var cur = extSettings[extId];
  if (cur && cur.installation_mode === 'removed') {
    result.already_present = true;
  }

  // Idempotency: if the correct policy entry is already set, nothing to write.
  // Re-reading the live plist each run ensures we react if it was externally removed.
  // Still self-heal perms: a prior run (or RTR's 077 umask) may have left the file
  // 0600 root-owned, which Chrome (running as the user) cannot read - so the policy
  // would be silently ignored despite being "present". Force readable every run.
  if (result.already_present) {
    _setMode(path, '644');
    var hpDir = path.substring(0, path.lastIndexOf('/'));
    if (hpDir) _setMode(hpDir, '755');
    return result;
  }

  // Mutate: force-remove this id. Preserve every sibling policy + sibling ext entry.
  extSettings[extId] = { installation_mode: 'removed' };
  policy.ExtensionSettings = extSettings;

  if (dryRun) return result;

  // Create the parent dir (system: /Library/Managed Preferences; per-user adds
  // the /<console-user> leaf, which often does not exist yet on a fresh box).
  var parentDir = path.substring(0, path.lastIndexOf('/'));
  if (parentDir && !isDirectory(parentDir)) {
    _fm.createDirectoryAtPathWithIntermediateDirectoriesAttributesError(parentDir, true, $(), null);
  }
  var wr = writePlistDict(path, policy);
  result.written = wr.ok;
  if (!wr.ok) {
    result.error = 'POLICY_WRITE_FAILED - ' + wr.error;
    writeDiag('ERROR: policy write failed (' + scope + '): ' + wr.error);
    return result;
  }

  // CRITICAL: make the policy world-readable and the dir traversable. RTR runs
  // as root with a restrictive umask (077), so writeToFileAtomically produces a
  // 0600 root-owned file and a 0700 dir - Chrome runs as the LOGGED-IN USER and
  // cannot read them, so the policy is silently ignored. Force 0644 file / 0755
  // dir so any user's Chrome can read the managed prefs.
  _setMode(path, '644');
  if (parentDir) _setMode(parentDir, '755');
  return result;
}

// Set POSIX permissions on a path via chmod (deterministic; avoids JXA attribute
// dict bridging). modeStr is an octal string, e.g. '644'.
function _setMode(path, modeStr) {
  var r = _runCommand('/bin/chmod', [modeStr, path]);
  if (r.exitCode !== 0) {
    writeDiag('WARN: chmod ' + modeStr + ' failed on ' + path + ': ' + (r.stderr || '').trim());
  }
}

// Per-user managed-preferences path for a given console user + browser bundle.
function perUserManagedPrefs(browser, user) {
  return '/Library/Managed Preferences/' + user + '/' + browser.bundleId + '.plist';
}

// ===== Section 6: Layer 2 - Filesystem Removal =====

// Validate a Glow-supplied path before deleting. Must resolve safely, sit under
// the expected browser User Data dir, be under a known per-profile ext-data
// subdir, and have the extension id as its leaf. Returns canonical path or null.
function validateExtDataPath(path, browser, extId) {
  var resolved = resolveSafe(path);
  if (!resolved) return null;

  // Must be under the browser's User Data dir (catches wrong-browser paths).
  if (resolved.indexOf('/' + browser.userDataRel) === -1) return null;

  // Belt-and-suspenders: also check Application Support as a category guard.
  if (resolved.indexOf('/Library/Application Support/') === -1) return null;

  // Leaf must equal the extension id - guards against traversal / wrong depth.
  var leaf = resolved.split('/').pop();
  if (leaf !== extId) return null;

  // Must be under a known per-profile extension data subdir.
  var hasSubdir = false;
  for (var i = 0; i < PROFILE_EXT_DATA_SUBDIRS.length; i++) {
    if (resolved.indexOf('/' + PROFILE_EXT_DATA_SUBDIRS[i] + '/' + extId) !== -1) {
      hasSubdir = true;
      break;
    }
  }
  if (!hasSubdir) return null;
  return resolved;
}

// Given an Extensions/<id> path, derive sibling data dirs in the same profile.
function _siblingDataPaths(extensionsPath, extId) {
  // .../Profile N/Extensions/<id>  >  profileDir = up 2 levels
  var parts = extensionsPath.split('/');
  parts.pop();           // <id>
  var subdir     = parts.pop(); // Extensions
  var profileDir = parts.join('/');
  var out = [];
  if (subdir === 'Extensions') {
    out.push(profileDir + '/Local Extension Settings/' + extId);
    out.push(profileDir + '/Sync Extension Settings/' + extId);
  }
  return out;
}

// Enumerate Extensions/<id> dirs across all users' Chrome profiles (used when
// Glow gives only the id, no paths). Always tries /Users/* first (works when
// running as root via RTR); falls back to NSHomeDirectory() if /Users/* is empty.
//
// Known limitation - TCC (macOS Sonoma 14+ / Sequoia 15+):
//   On Sonoma and later, root alone no longer bypasses TCC for ~/Library access.
//   If the RTR agent (CrowdStrike Falcon) does not have Full Disk Access granted
//   via MDM, listDirectory on ~/Library/Application Support will return empty
//   silently. The FS layer will then find nothing (targets_found=0) and the
//   policy layer remains the only active removal mechanism.
//   Detection: if targets_found=0 but policy was written, suspect TCC blocking.
//
// Note: $.getuid() is not available in JXA on macOS Ventura even with
//   ObjC.import('stdlib') - omit that check and always enumerate /Users/*.
function _discoverExtPaths(browser, extId) {
  var found = [];
  var homes = [];
  try {
    var users = listDirectory('/Users');
    for (var i = 0; i < users.length; i++) {
      var d = '/Users/' + users[i];
      if (users[i] !== 'Shared' && users[i].charAt(0) !== '.' && isDirectory(d)) {
        homes.push(d);
      }
    }
    if (!homes.length) {
      // Fallback for non-root or non-standard home layout.
      var h = $.NSHomeDirectory().js;
      if (h && isDirectory(h)) homes.push(h);
    }
  } catch (e) {}

  if (!homes.length) {
    writeDiag('WARN: no user home dirs found - TCC may be blocking /Users access');
  }

  for (var j = 0; j < homes.length; j++) {
    var userData = homes[j] + '/' + browser.userDataRel;
    if (!isDirectory(userData)) continue;
    var profiles = listDirectory(userData);
    for (var k = 0; k < profiles.length; k++) {
      if (!PROFILE_NAME_RE.test(profiles[k])) continue;
      var extDir = userData + '/' + profiles[k] + '/Extensions/' + extId;
      if (isDirectory(extDir)) found.push(extDir);
    }
  }
  return found;
}

function removeFilesystem(browser, extId, suppliedPaths, dryRun) {
  var result = { layer: 'filesystem', removed: [], skipped: [], changed: false, targets_found: 0 };

  // Build the target set: supplied Extensions paths (+ their siblings), or discover.
  var primary = [];
  if (suppliedPaths && suppliedPaths.length) {
    for (var i = 0; i < suppliedPaths.length; i++) {
      if (typeof suppliedPaths[i] === 'string') primary.push(suppliedPaths[i]);
    }
  }
  if (!primary.length) primary = _discoverExtPaths(browser, extId);

  var targets = [];
  for (var a = 0; a < primary.length; a++) {
    targets.push(primary[a]);
    var validated = resolveSafe(primary[a]);
    if (validated && validated.split('/').pop() === extId) {
      var sibs = _siblingDataPaths(validated, extId);
      for (var b = 0; b < sibs.length; b++) targets.push(sibs[b]);
    }
  }

  for (var t = 0; t < targets.length; t++) {
    var raw  = targets[t];
    var safe = validateExtDataPath(raw, browser, extId);
    if (!safe) {
      result.skipped.push({ path: raw, reason: 'failed_validation' });
      continue;
    }
    if (!fileExists(safe)) {
      result.skipped.push({ path: safe, reason: 'not_found' });
      continue;
    }
    result.targets_found++; // path exists - counts whether we delete or dry_run
    if (dryRun) {
      result.skipped.push({ path: safe, reason: 'dry_run' });
      continue;
    }
    if (removeItem(safe)) {
      result.removed.push(safe);
      result.changed = true;
    } else {
      result.skipped.push({ path: safe, reason: 'delete_failed' });
      result.error = 'DELETE_FAILED';
    }
  }
  return result;
}

// ===== Section 7: Input Decode & Main =====

function getProp(obj, name, dflt) {
  if (obj === null || typeof obj !== 'object') return dflt;
  return Object.prototype.hasOwnProperty.call(obj, name) && obj[name] != null ? obj[name] : dflt;
}

function castBool(v, dflt) {
  if (typeof v === 'boolean') return v;
  if (typeof v === 'string')  return v === 'true';
  return dflt;
}

// Extract a Chrome ext id from a supplied path leaf, if the leaf matches the id
// format. Used when extension_id is absent from input but paths are provided.
function _extIdFromPaths(paths) {
  if (!paths) return null;
  for (var i = 0; i < paths.length; i++) {
    if (typeof paths[i] !== 'string') continue;
    var leaf = paths[i].replace(/\/+$/, '').split('/').pop();
    if (EXT_ID_RE.test(leaf)) return leaf;
  }
  return null;
}

function run(argv) {
  var startTime = nowIso();
  var dryRun    = true; // safe default for bare local invocation
  var envelope;

  try {
    var input = null;
    if (argv && argv.length > 0 && argv[0]) {
      var data    = $.NSData.alloc.initWithBase64EncodedStringOptions(argv[0], 0);
      var decoded = $.NSString.alloc.initWithDataEncoding(data, $.NSUTF8StringEncoding).js;
      input        = JSON.parse(decoded);
      dryRun = castBool(getProp(input, 'dry_run', false), false);
    }
    var params = getProp(input, 'params', {});

    var browserKey = String(getProp(params, 'browser', 'chrome')).toLowerCase();
    var browser    = BROWSERS[browserKey];
    if (!browser) {
      throw { code: 'UNSUPPORTED_BROWSER', message: 'browser not supported: ' + browserKey };
    }

    var suppliedPaths = getProp(params, 'installation_paths', null);
    if (suppliedPaths == null) {
      var single = getProp(params, 'installation_path', null);
      if (single) suppliedPaths = [single];
    }
    if (suppliedPaths != null && !Array.isArray(suppliedPaths)) suppliedPaths = [suppliedPaths];

    var extId = getProp(params, 'extension_id', null);
    if (extId == null && suppliedPaths) extId = _extIdFromPaths(suppliedPaths);
    extId = (extId == null) ? '' : String(extId);
    if (!EXT_ID_RE.test(extId)) {
      throw { code: 'INVALID_PARAMS', message: 'invalid or missing extension_id' };
    }

    // Telemetry only - the policy mechanism no longer branches on version.
    // Chrome reads /Library/Managed Preferences/ as Mandatory on every macOS
    // version; the historical "Chrome 119+ ignores it" was a pruned-orphan bug,
    // not an ignored-policy bug. We enforce on all versions via managed prefs.
    var osMajor     = getOSMajorVersion();
    var chromeMajor = getBrowserMajorVersion(browser);

    // Resolve the active GUI user up front - needed for the per-user policy and
    // the in-session relaunch. null => no real user logged in (login screen /
    // headless): skip per-user + relaunch; system policy + FS layer still run.
    var consoleUser = resolveConsoleUser();

    // ---- Layer 1: managed-preferences policy (both scopes, both Mandatory) ----
    // System scope: Machine/Mandatory. Persists + blocks ALL users pre-Sonoma;
    //   enforced-while-present on Sonoma (managedclient prunes it at reboot).
    var sysPolicy = applyRemovalPolicy(
      browser, extId, browser.managedPrefs, 'system', dryRun);
    // Per-user scope: Current-user/Mandatory. SURVIVES the active login session
    //   on Sonoma - the path that makes RTR-only removal work without MDM.
    var userPolicy = consoleUser
      ? applyRemovalPolicy(
          browser, extId, perUserManagedPrefs(browser, consoleUser.user), 'user', dryRun)
      : null;

    var policyWritten = sysPolicy.written || (userPolicy ? userPolicy.written : false);
    var policyPresent = sysPolicy.already_present || (userPolicy ? userPolicy.already_present : false);
    var policyInPlace = policyWritten || policyPresent;

    // Flush cfprefsd so Chrome reads fresh policy on relaunch. Managed prefs are
    // written under cfprefsd's nose; without a flush it serves the stale cache.
    // Flush whenever a policy is in place (not only on fresh writes): an
    // idempotent run may have just healed the file perms from 0600 to 0644, and
    // the user's cfprefsd may still be caching the prior "unreadable -> absent"
    // result, so it must be refreshed before Chrome relaunches.
    var cpflush = policyInPlace ? flushPolicyCache(dryRun) : { flushed: false };

    // ---- Kill -> filesystem removal -> in-session relaunch ----
    // Order matters. A managed-prefs policy is honored by Chrome on every macOS
    // version, so when a policy is in place we:
    //   1) kill Chrome,
    //   2) delete the extension files across all users/profiles WHILE Chrome is
    //      dead - deleting them under a live Chrome makes it flag the extension
    //      "corrupted" and repair/redownload it, undoing the removal,
    //   3) relaunch Chrome inside the user's GUI session so the policy engine
    //      reads the fresh rule and force-uninstalls immediately, before any
    //      managedclient prune - rather than waiting on the user to reopen it.
    // The FS layer is what makes a "removed" extension STAY gone even after the
    // orphan managed-prefs file is later pruned at a session boundary.
    var kill;
    var fs;
    var relaunch = null;
    if (policyInPlace) {
      kill = killBrowser(browser, dryRun);
      fs   = removeFilesystem(browser, extId, suppliedPaths, dryRun);
      // Relaunch only when a real user is logged in AND Chrome had been running.
      // If Chrome was closed there is nothing to disrupt; policy still applies
      // when the user next opens it, and the FS layer already removed the files.
      if (consoleUser && kill.was_running) {
        relaunch = relaunchBrowser(browser, consoleUser.uid, dryRun);
      }
    } else {
      // No enforceable policy landed. Still attempt FS cleanup, but do NOT kill:
      // a kill+relaunch with no blocking policy lets Chrome re-sync the extension
      // back from Chrome Sync, undoing the FS removal.
      fs = removeFilesystem(browser, extId, suppliedPaths, dryRun);
      var runCheck = _runCommand('/usr/bin/pgrep', ['-x', browser.processName]);
      kill = {
        process:        browser.processName,
        was_running:    runCheck.exitCode === 0,
        killed:         false,
        exited_cleanly: false,
        kill_skipped:   true,
        reason:         'policy_not_enforced',
      };
    }

    // anythingToDo: true if any layer had real work - a new policy entry to write
    // (on either scope), or FS targets to delete.
    var policyNeedsWrite = !sysPolicy.already_present || (userPolicy ? !userPolicy.already_present : false);
    var fsHasTargets     = fs.targets_found > 0;
    var anythingToDo     = policyNeedsWrite || fsHasTargets;
    var changed = fs.changed || policyWritten;

    // Failure model: managed prefs is the enforced layer. A write failure counts
    // as a policy failure only when EVERY written scope failed - one honored
    // scope is enough to enforce. FS failure is always reported.
    var sysFailed    = !!sysPolicy.error;
    var userFailed   = userPolicy ? !!userPolicy.error : false;
    var policyFailed = userPolicy ? (sysFailed && userFailed) : sysFailed;
    var fsFailed     = !!fs.error;

    var status = 'success';
    var error  = null;
    if (policyFailed || fsFailed) {
      status = 'failure';
      var errCode, msg;
      if (policyFailed) {
        errCode = sysPolicy.error || (userPolicy && userPolicy.error) || 'POLICY_WRITE_FAILED';
        msg     = 'managed-prefs policy write failed on all scopes - ' + JSON.stringify(sysPolicy) + " *** " + JSON.stringify(userPolicy);
      } else {
        errCode = fs.error;
        msg     = 'filesystem layer failed';
      }
      error = { code: errCode, message: msg, stderr: '' };
    } else if (!anythingToDo) {
      // All layers found nothing to do.
      status  = 'skipped';
      changed = false;
    }

    envelope = {
      os_family:             OS_FAMILY,
      script_version:        SCRIPT_VERSION,
      status:                status,
      changed:               changed,
      error:                 error,
      dry_run:               dryRun,
      start_time:            startTime,
      end_time:              nowIso(),
      metadata:              { hostname: hostname(), serial_number: serialNumber() },
      extension_id:          extId,
      browser:               browserKey,
      os_major_version:      osMajor,
      browser_version:       chromeMajor,
      policy_source:         'managed_prefs',
      console_user:          consoleUser ? consoleUser.user : null,
      system_policy:         sysPolicy,
      user_policy:           userPolicy,
      cfprefsd_flushed:      cpflush.flushed || false,
      cfprefsd_back:         cpflush.cfprefsd_back || false,
      processes_stopped:     [kill],
      relaunch:              relaunch,
      filesystem:            fs,
    };
  } catch (e) {
    var code = (e && e.code)    ? e.code    : 'UNHANDLED_ERROR';
    var msg  = (e && e.message) ? e.message : String(e);
    writeDiag('ERROR: ' + code + ': ' + msg);
    envelope = {
      os_family:      OS_FAMILY,
      script_version: SCRIPT_VERSION,
      status:         'failure',
      changed:        false,
      error:          { code: code, message: msg, stderr: '' },
      dry_run:        dryRun,
      start_time:     startTime,
      end_time:       nowIso(),
      metadata:       { hostname: hostname(), serial_number: '' },
    };
  }

  var json = JSON.stringify(envelope);
  writeDiag('RESULT: ' + json);
  return json;
}

// osascript invokes the global run(argv) automatically; its return value is
// printed to stdout. That is the only thing this script writes to stdout.
