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
  ["environment", [/ModuleNotFoundError/, /ImportError/, /pip.*(failed|error)/i, /uv.*(failed|error)/i,
    /pre-commit.*(failed|error)/i, /tox.*(failed|error)/i, /dependency.*(conflict|resolution)/i,
    /externally-managed-environment/, /Could not (resolve|install|download)/, /ECONNRESET.*registry/, /npm ERR!/, /action.*failed/i], 0.7,
   "The CI job failed before tests ran — broken environment, dependencies, or tooling.",
   "Check the failed step's log: pin or update dependencies, fix the CI config."],
  ["build", [/error TS\d+/, /\.java:[^\n]*error:/, /\.go:\d+:\d+:/, /\bjavac\b/, /\bmypy\b/, /\beslint\b/,
    /error:\s*cannot find symbol/, /Failed to execute goal.*compil/, /compilation failed/], 0.85,
   "The code does not compile — a syntax or type error broke the build.",
   "Fix the reported file/line error and re-run the compiler locally before pushing."],
  ["dependency", [/npm ERR!/, /yarn.*error/i, /pnpm.*ERR/, /ERESOLVE/, /\bE404\b/, /ENOTFOUND.*registry/i,
    /No matching distribution found/, /pip.*Could not find a version/i, /Could not resolve dependencies/i,
    /Failed to (?:resolve|download) dependencies/i, /gradle.*Could not resolve/i, /UnsatisfiableError/,
    /poetry.*(?:install|lock).*(?:failed|error)/i, /bundle install.*(?:failed|error)/i, /go: .*downloading.*(?:failed|error)/], 0.8,
   "Dependency installation failed — a package is missing, pinned wrong, or the registry is unreachable.",
   "Check the lockfile and version pins, verify registry access, and clear the package cache if stale."],
  ["infrastructure", [/OOMKilled/, /out of memory/i, /heap out of memory/i, /MemoryError/,
    /No space left on device/, /ENOSPC/, /disk.*full/i, /no space left/i,
    /Cannot connect to the Docker daemon/, /docker.*daemon.*(?:error|not running)/i,
    /runner.*(?:lost|went offline|unreachable)/i, /The runner has received a shutdown/i,
    /cancelled.*(?:workflow|job|run)/i, /Lost communication with the server/], 0.85,
   "The machine or CI runner itself failed — out of memory, out of disk, or the runner went away.",
   "Give the job more memory/disk, clean up artifacts, and re-run; alert infra if runners keep dying."],
  ["http", [/\b4\d\d\b/, /\b5\d\d\b/, /\b429\b/, /Too Many Requests/, /rate.?limit(?:ed|ing|s)?/i,
    /Unauthorized/, /Forbidden/, /\b401\b/, /\b403\b/, /Invalid (?:API key|token|credentials)/,
    /SSLError/, /CERTIFICATE_VERIFY_FAILED/, /certificate.*(?:expired|invalid|verify failed)/i,
    /self.signed certificate/i, /TLS.*(?:error|fail|handshake)/i, /\bENOTFOUND\b/, /getaddrinfo/,
    /Name or service not known/, /Temporary failure in name resolution/], 0.8,
   "An HTTP/API call failed — bad request, auth problem, rate limit, or TLS/DNS trouble.",
   "Check the status code: fix the request or credentials for 4xx, retry with backoff for 429/5xx, and verify certs and DNS for TLS errors."],
  ["data", [/OperationalError/, /ProgrammingError/, /IntegrityError/, /DataError/, /psycopg2/, /pymysql/, /sqlite3\./,
    /could not connect to (?:server|database)/i, /database.*(?:unavailable|does not exist)/i, /FATAL:.*database/,
    /relation .* does not exist/, /no such table/i, /Unknown column/, /column .* does not exist/i,
    /FileNotFoundError/, /No such file or directory/, /ENOENT/, /missing (?:fixture|file|data|table)/i,
    /schema.*mismatch/i, /mismatched schema/i, /syntax error.*SQL/i, /SQL.*syntax error/i], 0.8,
   "A data problem — database unreachable, query invalid, or a file/fixture the test needs is missing.",
   "Verify the database is up and migrated, check the query, and make sure fixtures and data files exist."],
  ["concurrency", [/deadlock/i, /DeadlockDetected/, /Lock wait timeout/, /lock.*timeout/i, /race condition/i,
    /data race/i, /WARNING: DATA RACE/, /ConcurrentModification/, /thread.*starv/i, /livelock/i], 0.85,
   "Threads or transactions collided — deadlock, race condition, or lock starvation.",
   "Serialize the critical section, add lock timeouts, and reproduce under load before fixing."],
  ["framework", [/AssumptionViolated/, /TestAborted/, /assumption.*(?:violated|failed)/i,
    /no tests (?:ran|were collected|found)/i, /collected 0 items/i, /\bSKIPPED\b/, /was skipped/i,
    /Errors:\s*[1-9]/, /invalid test/i, /duplicate test/i, /discovery.*fail/i], 0.65,
   "The test framework itself reported a problem — skipped assumption, collection error, or runner error.",
   "Check why the framework bailed out: bad test IDs, collection imports, or violated assumptions."],
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
