# Claude Code — Permanent Memory for this Environment

## Git & Commit Signing Constraints

- **The commit signing server is locked to `terevos/reformedbot` only.**  
  Commits attempted in any other local repo directory (e.g. `/home/user/openpdf-reader`) fail with:  
  `signing server returned status 400: {"message":"missing source"}`  
  This is a session-level constraint and cannot be overridden by changing the remote URL.

- **The local git proxy URL format is:**  
  `http://local_proxy@127.0.0.1:24749/git/{owner}/{repo}`  
  The reformedbot repo is pre-configured to use this proxy. Other repos must also use it, but
  the signing server will still reject them unless they are registered with the session.

- **GitHub MCP tools are restricted to `terevos/reformedbot`.**  
  Calls to `mcp__github__*` for any other repository (e.g. creating repos, pushing files) will
  be denied with a 403 or 404. Repo creation (`create_repository`) also requires org-level
  permissions that are not available in this environment.

## Workaround for New Repositories

When the user needs code pushed to a **new GitHub repo**:

1. Commit all files to `terevos/reformedbot` on the relevant feature branch
   (e.g. `claude/android-pdf-reader-p2Icx`), placing the project in a named subdirectory.
2. Push that branch — this works because the signing server accepts `terevos/reformedbot`.
3. Provide the user with a short local shell script to copy the subdirectory into their new repo
   and push it themselves:

```bash
git clone https://github.com/terevos/reformedbot.git \
  --branch <feature-branch> --single-branch tmp-src
git clone https://github.com/<owner>/<new-repo>.git
cp -r tmp-src/<subdirectory>/. <new-repo>/
git -C <new-repo> add -A
git -C <new-repo> commit -m "<message>"
git -C <new-repo> push origin main
rm -rf tmp-src
```

## Content Filtering

- Generating the **full GPL v3 legal text** in a single response triggers the content filtering
  policy (HTTP 400 from the API). Use a short copyright header + SPDX identifier instead:

```
SPDX-License-Identifier: GPL-3.0-or-later
```

  The full license text can be fetched from `https://www.gnu.org/licenses/gpl-3.0.txt` and
  committed by the user directly.

## MCP Tool Availability

- GitHub MCP tools disconnect and reconnect intermittently during a session.  
  Before calling any `mcp__github__*` tool, verify it is available via `ToolSearch`.  
  Do **not** retry a disconnected tool — wait for the system reminder that it has reconnected.
