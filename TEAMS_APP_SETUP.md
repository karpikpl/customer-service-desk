# Connect Customer Service Desk to Microsoft Teams

Deploy the agent through `azd` and enable its Teams channel before packaging.
Use your own deployed bot identity; no bot IDs or tenant-specific app packages
are committed to this repository.

Generate a personal-scope package:

```sh
azd ai agent pack service-desk --scope personal \
  --display-name "Customer Service Desk" --output-dir ./dist/teams
```

Inspect the generated manifest before installation: bot scopes must be
`["personal"]`, `supportsFiles` must be `false`, and `copilotAgents` must be
absent. Repackage the corrected manifest and icons at the ZIP root.
See the [packaging runbook](README.md#personal-teams-app-package).

Install using one of the methods below, subject to your tenant's app policies.

## Sideload — Teams UI (no extra tooling)

1. In Teams, go to **Apps** -> **Manage your apps** -> **Upload an app**.
2. Select **Upload a custom app**, choose `dist/teams/appPackage.zip`, then **Add**.
3. Select **Open**, then send a message to talk to your agent.

Upload a custom app guide: https://learn.microsoft.com/microsoftteams/platform/concepts/deploy-and-publish/apps-upload

## Or sideload — command line (atk)

The Microsoft 365 Agents Toolkit CLI (atk) installs the same package from a terminal.
`--scope Personal` requests a per-user install; tenant policy still applies:

```sh
npm install -g @microsoft/m365agentstoolkit-cli   # one-time; requires Node.js
atk auth login                                     # sign in with your M365 account
atk install --file-path dist/teams/appPackage.zip --scope Personal
```

atk prints a TitleId and a Teams deep link you can open to launch the agent.
atk CLI reference: https://learn.microsoft.com/microsoftteams/platform/toolkit/microsoft-365-agents-toolkit-cli

If **Upload a custom app** is missing or greyed out, custom app upload is turned off for
your tenant, or you want everyone in your org to get it from the org app catalog. Both need
a Teams admin: https://learn.microsoft.com/microsoftteams/platform/concepts/build-and-test/prepare-your-o365-tenant
