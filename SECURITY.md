# Security policy

## Reporting a vulnerability

Use **GitHub's private vulnerability reporting**: on the repository page open the
**Security** tab, choose **Report a vulnerability**, and fill in the form. Only the
maintainers can see it, and the conversation, any fix and the credit stay in that one
private advisory. Please do not open a public issue, pull request or discussion for a
vulnerability, and do not post a proof of concept anywhere public before it is fixed.

If the Security tab does not offer **Report a vulnerability**, the repository has not
turned the feature on: open an issue that says only that you have a security report to
make (no details), and a maintainer will open a private channel.

Include, as far as you can:

- what you found and where (file, function, config key or endpoint);
- the version or commit it affects;
- how to reproduce it, ideally a failing test or a minimal config and suite;
- what an attacker gains, and what they need to already have.

You will get an acknowledgement within 5 working days. We will tell you whether we
accept the report as a vulnerability, keep you informed while it is fixed, and credit
you in the release notes unless you ask us not to. Please give us up to 90 days to
ship a fix before you disclose publicly; if it is being actively exploited, tell us
and we will move faster.

## Supported versions

This is pre-1.0 software. Only the latest release receives security fixes.

| Version | Supported |
|---|---|
| 0.1.x (latest) | yes |
| anything older | no |

## What counts

The framework grades other systems and writes reports about them, so the things that
matter most are the ones that make a report lie or leak. These are in scope:

- **A check that passes on input it never received.** A missing field, status or
  tenant that reads as success. The framework treats this as a defect, not a
  scoring quirk: an unmeasurable case must come out `skipped` or `error`, never
  `pass` (see the invariants in [CONTRIBUTING.md](CONTRIBUTING.md)).
- **A report that carries what it should not.** A PII or credential match, or a
  retrieved chunk, surviving `ci` redaction; `full` redaction being honoured when
  `CI` is set; a secret appearing in any file the framework writes.
- **A cache or baseline that can be poisoned or that stores sensitive text.** The
  judge's verdict cache holds scores only, because it is restorable by other
  workflows; anything that puts reply text back into it is a vulnerability.
- **Path traversal or injection** through run names, report names, suite files,
  config values or the HTTP adapter's request template and response mapping.
- **Credential handling.** Anything that reads a credential from a place it should
  not, writes one to a log or report, or sends one to the wrong host.
- **The bundled dashboard and HTTP service** (`askback serve`): anything that lets a
  request write the corpus, the intent catalogue or the baseline, or rebuild the
  engine when `server.allow_runtime_reload` is off.

## What does not

- Findings about the **simulator** (`src/askback_sim/`) that its own test suites report,
  such as an invented policy or an unrouted escalation. The simulator is a
  development fixture with known failures, listed in
  [examples/hr/README.md](examples/hr/README.md). It is not meant to be deployed.
- Deploying `askback serve` on an open network with no `ASKBACK_API_KEY` and
  `cors_origins: ["*"]`. Those are the local defaults, and the configuration file
  says to change them before exposing the service.
- Spend. A hosted judge is metered by its provider, and `budget_usd` is an estimate
  checked between cases. Overshooting it by the cases in flight is documented
  behaviour (see [docs/cost.md](docs/cost.md)).
- Vulnerabilities in a dependency with no way to trigger them through this project.
  Report those to the dependency.

## Handling secrets when you use the framework

- Credentials come from the environment (`${VAR}` references in the config), never
  from a file in the repository. The framework never gives a credential a default.
- Reports are written at `ci` redaction unless you change it. Use `metrics_only`
  when the agent's answers are themselves sensitive, as they are for HR data.
- Treat everything under `reports/` and `.cache/` as sensitive until you have
  looked at it, whatever the redaction setting says.
