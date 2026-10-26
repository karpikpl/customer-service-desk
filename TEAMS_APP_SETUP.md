# Connect Customer Service Desk to Microsoft Teams

The demo is deployed through `azd` and the connected test account has the
**Customer Service Desk 1.0.1** personal app installed. Runtime version 33 serves
the red Customer logo in its Adaptive Cards.

The verified installation preserved its hosted agent, bot identity, and
conversation history. The checked-in manifest now uses backend name
`customer-service-desk`; this source rename has not been deployed or migrated.
For an existing installation with a different backend name, reconcile the
manifest with its actual deployment before deploying. Renaming source settings
alone does not reconnect the installed app or migrate conversation state.

## Runtime branding and installed-app branding are separate

Deploying the Python image updates the persona and card masthead. The Teams app
name, color icon, and outline icon come from its installed app package; they do
not change merely because a new agent image is deployed.

The original red Customer assets are checked in at:

- `src/service_desk/assets/customer-logo.png`: card masthead.
- `assets/teams/color.png`: 192 x 192 Teams color icon.
- `assets/teams/outline.png`: 32 x 32 transparent white outline icon.

The validated current package is
`dist/teams/customer-1.0.1/appPackage.zip`, also copied to
`dist/teams/appPackage.zip`. Generated packages are ignored by Git.

## Install or update through Teams

1. In Teams, open **Store** (called **Apps** in some clients), then
   **Manage your apps** -> **Upload an app**.
2. Select **Upload a custom app** and choose the validated ZIP.
3. Select **Add** or **Update**, then **Open** when installation succeeds.
   The connected client displayed **Add**, even for this same-ID update.
4. Check that the app header says **Customer Service Desk**. Send `/agent` to
   inspect the independently deployed runtime version and confirmation mode.

Preserve the existing manifest app ID and bot ID when updating. Do not uninstall
the old app or create a new bot just to change its branding. Historical messages
may still contain their old card artwork or accessibility labels.

## Rebuild a package for another update

Use the service key from `azure.yaml`, not the backend resource name:

```bash
azd ai agent pack service-desk \
  --display-name "Customer Service Desk" \
  --app-version 1.0.1 \
  --scope personal \
  --output-dir dist/teams
```

Bump the app version for subsequent changes. Native packaging can regenerate
default icons and broader manifest settings: replace its icons with the checked-in
Customer assets and verify the final ZIP before uploading. Keep the manifest's
app/bot IDs unchanged, bot scopes exactly `["personal"]`, file support disabled,
and no `copilotAgents` entry. `--scope personal` controls distribution and does
not by itself guarantee the bot's installation scopes.

See the [README](README.md) for deployment, demo scenarios, flow budgets, and the
stable persistence settings used by this existing agent.

If **Upload a custom app** is missing or greyed out, custom app upload is turned off for
your tenant. Ask a Teams administrator to approve the appropriate policy or org
catalog distribution; do not bypass it.

- [Upload custom apps](https://learn.microsoft.com/microsoftteams/platform/concepts/deploy-and-publish/apps-upload)
- [Prepare the tenant](https://learn.microsoft.com/microsoftteams/platform/concepts/build-and-test/prepare-your-o365-tenant)
