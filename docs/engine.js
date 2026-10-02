/* Offline triage engine — browser port of src/rule_triage.py + src/parsers.py. */

const PATTERNS = [
  ["timeout", [/\btimed?\s?out\b/i, /TimeoutException/, /wait.*exceeded/i, /deadline exceeded/i], 0.9,
   "The test waited for something that never completed — slow page, hung service, or too-short wait.",
   "Increase the explicit wait, check for backend slowness, and confirm the app reaches a ready state."],
  ["locator", [/NoSuchElement/, /Unable to locate element/i, /StaleElementReference/, /locator/i, /selector.*not found/i], 0.9,
   "A UI locator no longer matches — the page changed or the element loads late.",
   "Update the selector to a stable attribute and add an explicit wait for visibility."],
  ["connection", [/ConnectionError/, /connection refused/i, /Max retries exceeded/, /ECONNREFUSED/, /Failed to establish/i, /socket.*timeout/i, /\b50[234]\b/], 0.85,
   "The test could not reach a service — network, DNS, or the service is down.",
   "Check service health and test-environment networking; add retries for transient blips."],
  ["assertion", [/AssertionError/, /\bassert\b/i, /expected.*but (?:was|got)/i, /mismatch/i], 0.8,
   "An assertion failed — the app behaved differently than the test expects.",
   "Verify whether the app changed intentionally (update the test) or this is a real regression."],
  ["setup", [/error in setup/i, /fixture.*error/i, /setUp.*(error|fail)/i, /before.*hook.*fail/i], 0.75,
   "The test never really ran — setup or teardown failed.",
   "Fix the fixture/environment setup; check test data and service prerequisites."],
];

function classify(message, traceback) {
  const blob = (message || "") + "\n" + (traceback || "");
  let best = { category: "unknown", confidence: 0.4, evidence: [] };
  for (const [category, regexes, confidence] of PATTERNS) {
    const hits = regexes.filter(rx => rx.test(blob)).map(rx => rx.source);
    if (hits.length && confidence > best.confidence) best = { category, confidence, evidence: hits };
  }
  return best;
}

function rootCauseAndFix(category) {
  const p = PATTERNS.find(p => p[0] === category);
  return p ? [p[3], p[4]] : ["Could not determine the failure type from the available output.",
    "Re-run with full logs and stack traces, then triage again."];
}

function parseFailures(text) {
  const failures = [];
  const lineRe = /^(FAILED|ERROR)\s+(\S+?)\s*-\s*(.*)$/;
  for (const line of text.split("\n")) {
    const m = line.trim().match(lineRe);
    if (m) failures.push({ test_id: m[2], message: m[3].trim(), traceback: "" });
  }
  // stack-trace blocks
  for (const block of text.split(/(?=Traceback \(most recent call last\))/g).slice(1)) {
    const lines = block.trim().split("\n").filter(l => l.trim());
    if (!lines.length) continue;
    failures.push({ test_id: `log-trace-${failures.length + 1}`, message: lines[lines.length - 1].trim(), traceback: block.trim() });
  }
  // attach tracebacks to FAILED lines when the id appears in a trace block
  return failures;
}

function triageFailures(failures) {
  return failures.map(f => {
    const { category, confidence, evidence } = classify(f.message, f.traceback);
    const [root_cause, suggested_fix] = rootCauseAndFix(category);
    return { ...f, category, confidence, evidence, root_cause, suggested_fix,
             priority: category === "connection" ? "P1" : category === "assertion" ? "P2" : "P3" };
  });
}

function summarize(results) {
  const byCat = {}, byPrio = {};
  results.forEach(r => { byCat[r.category] = (byCat[r.category] || 0) + 1; byPrio[r.priority] = (byPrio[r.priority] || 0) + 1; });
  return { total: results.length, by_category: byCat, by_priority: byPrio };
}

if (typeof module !== "undefined") module.exports = { classify, parseFailures, triageFailures, summarize };
