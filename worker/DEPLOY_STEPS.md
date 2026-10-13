# Deploy the "BOTLAB Bot Log Auditor" demo (Cloudflare Worker)

The demo runs on its own on Cloudflare, completely separate from any trading server, so it never touches exchange keys.
When you are done you get a link like `https://botlab-auditor.<account-subdomain>.workers.dev`.

## Requirements
- A Cloudflare account. The free plan is enough, and Workers AI is free within the daily allowance.
- Node.js installed. Check with `node -v`.
- A PayPal account is optional. Without one, the PayPal sandbox button with the public client id "sb" still works (test money only).

## Steps (PowerShell or any terminal)

**1. Go to the worker folder and install tools** (first time only)
```powershell
cd hackathon_auditor\worker
npm install
```

**2. Run the tests.** The result must be `pass 30` and `fail 0`.
```powershell
npm test
```

**3. Log in to Cloudflare** (first time only). A browser opens; click **Allow**.
```powershell
npx wrangler login
```

**4. Deploy**
```powershell
npx wrangler deploy
```
This prints the `https://botlab-auditor....workers.dev` link — the "Try it out" link for judges.
If Cloudflare asks for a workers.dev subdomain, any name works, e.g. `botlab`.

**5. Open the link to check**
- Click one of the "Try a sample" buttons.
- The agent steps appear one by one, then the 6-gate table and the report.
- The report source line must say "Workers AI". If it says "template", the AI call failed and the page fell back to the template report.

## (Optional) Real server-side PayPal sandbox capture
Only if you have a PayPal Developer account and a sandbox REST app:
```powershell
npx wrangler secret put PAYPAL_CLIENT_ID
npx wrangler secret put PAYPAL_CLIENT_SECRET
npx wrangler deploy
```
Each `secret put` asks for the value; paste it and press Enter. Secrets are stored on Cloudflare, never in a file.

## Turn PayPal off completely (mock unlock)
1. Change `PAYPAL_MODE = "sb"` to `PAYPAL_MODE = "mock"` in `wrangler.toml`.
2. Run `npx wrangler deploy` again.

## Update after code changes
```powershell
npm test
npx wrangler deploy
```

## Take the demo down
```powershell
npx wrangler delete
```

Notes:
- The free plan allows about 10 ms of CPU per request. Small sample files run fine; very large files (tens of thousands of trades) may hit the CPU limit and need the Workers Paid plan.
- Educational tool, not financial advice.
