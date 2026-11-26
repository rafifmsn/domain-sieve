# Hackathon Rules & Constraints

Distilled operational rules from `https://imphnen.dev/hackaton-anti-judol` and `https://imphnen.dev/hackaton-anti-judol/info#aturan` (retrieved Nov 26, 2026).

## Schedule (All Times WIB / UTC+7)

| Phase | Window | Operational Impact |
| :--- | :--- | :--- |
| Registration | Wed Nov 25, 00:00 to Fri Dec 4, 00:00 | Solo or team (max 4 members, unique name 2-40 chars); build pipeline, run A/B eval, collect initial queue |
| Submission Window | Sat Dec 5, 00:00 to Thu Dec 10, 00:00 | Submit form open; first team to claim a canonical key holds the point |
| Final Verification | Thu Dec 10, 00:00 to Mon Dec 14, 00:00 | Committee manually evaluates remaining entries |
| Winner Announcement | Mon Dec 14, 00:00 | Top 3 teams share Rp 55 million prize pool; Top 3 must submit a post-event write-up |
| Participant Survey | Closes Sun Dec 27, 00:00 | Mandatory participant survey |

## Scoring & Tie-Breaker

1. **1 Valid entry = 1 point.**
   Entries still in `Menunggu penilaian` do not count until evaluated as `Valid`.
2. **Tie-breaker:** If two teams end with equal points, the team that reached its final point earliest by **submission timestamp** (not evaluation timestamp) ranks higher.
3. **Human evaluation:** The committee inspects every entry manually (`Valid` or `Ditolak` with a displayed reason); there is no automated acceptance check at submit time.

## Entry Validity Constraints

Every submitted URL must satisfy all of the following rules:

- Scheme must be `http://` or `https://`.
- No port number (e.g., `:8080` is invalid).
- No embedded credentials (`user:pass@` is invalid).
- Must not be a raw IP address or `localhost`.
- Maximum total length of **300 characters**.
- Must represent illegal content relevant to Komdigi enforcement: online gambling (*judol*), illegal lending (*pinjol*), digital fraud/phishing, or other illegal web activity.
- Must follow Indonesian law: no unauthorized hacking, system destruction, account takeover, or service attacks against target sites.

## Entry Statuses

| Status | Points | Meaning |
| :--- | :--- | :--- |
| `Menunggu penilaian` | 0 (pending) | Submitted to queue; holds lock against global duplicates until reviewed |
| `Valid` | +1 | Accepted by committee; holds lock permanently |
| `Duplikat` | 0 | Canonical form already held by an active (`Menunggu penilaian` or `Valid`) entry from **any** team |
| `Ditolak` | 0 | Rejected by committee with reason shown; releases the canonical lock so it may be re-submitted if corrected |

Note: An entry marked `Duplikat` does **not** automatically revive if the earlier holding entry is later `Ditolak`.

## Exact Canonicalization & `Duplikat` Rules

Two URLs are considered identical by the platform if their normalized canonical forms match.
Apply transformations in this exact order:

1. Strip the scheme (`http://` or `https://`).
2. Strip a leading `www.` prefix from the hostname **only** (preserve all other subdomains such as `blog.tokobudi.id`).
3. Lowercase the hostname.
4. Strip any query string (`?...`) and fragment (`#...`).
5. Strip trailing `/` characters from the path.
6. Preserve the path **with exact case sensitivity**.

### Official Reference Test Vectors

| Raw Input | Expected Canonical Key | Notes |
| :--- | :--- | :--- |
| `https://www.TokoBudi.id/` | `tokobudi.id` | Strips scheme, `www.`, trailing `/`; lowercases host |
| `tokobudi.id` | `tokobudi.id` | Already canonical |
| `http://TOKOBUDI.id/?utm=ig#promo` | `tokobudi.id` | Strips query and fragment |
| `tokobudi.id/produk` | `tokobudi.id/produk` | Preserves path |
| `tokobudi.id/Produk` | `tokobudi.id/Produk` | Path is case-sensitive (distinct from `/produk`) |
| `blog.tokobudi.id` | `blog.tokobudi.id` | Subdomain other than `www.` is preserved |

## Submission Form Mechanics

- **Bulk submit mode:** 1 URL per line, maximum **20 links per submission**, with a **3-second cooldown** between submissions.
- **Notes:** Optional note up to **140 characters**, supported **only** in single-link submission mode.
- **Form pre-check:** The submission UI previews the recorded canonical form and warns if already claimed (shows claim timestamp only, never the claiming team name).
- **Provider AI Access:** Free 7-day access to DeepSeek Uncensored (partnered with Netra Runtime) is provided under event terms.
