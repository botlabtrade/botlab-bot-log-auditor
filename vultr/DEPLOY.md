# Deploy BOTLAB Bot Log Auditor on Vultr (step-by-step)

Takes about 30–45 minutes. You need a Vultr account with credit and, for the real AI, a Vultr **Inference API key**.
Every command below can be copy-pasted. There are two places to type commands:
- **Your machine (Windows PowerShell)**: lines starting with `PS>`. Do not type `PS>`.
- **The Vultr server (after SSH)**: lines starting with `#`. Do not type `#`.

> **Safety rule:** the API key is only ever typed into the `.env` file on the server. **Never paste a key into chat**, email, screenshots or GitHub.

---

## Step 0: Prepare (once)

1. Log in to https://my.vultr.com and check that the hackathon credit is in your account (*Billing*).
2. (For the real AI) Go to **Products → Serverless → Inference**, create a subscription, open it and copy the **API Key** somewhere safe (e.g. a password manager). This is the *Inference* key, not the account key.
3. (Optional) If you have a domain (e.g. `auditor.example.com`), you will point it at the server later for HTTPS. Without a domain it still works on `http://<IP>`.

## Step 1: Create an SSH key on Windows (once)

Open **PowerShell**:
```powershell
PS> ssh-keygen -t ed25519 -f $env:USERPROFILE\.ssh\vultr_botlab
```
Press Enter twice for an empty passphrase if you like. Then print the **public** key (safe to share):
```powershell
PS> Get-Content $env:USERPROFILE\.ssh\vultr_botlab.pub
```
Copy the whole line starting with `ssh-ed25519 ...`. In Vultr go to **Account → SSH Keys → Add SSH Key**, paste it and name it `botlab`.

## Step 2: Create the server (Cloud Compute)

In Vultr click **Deploy +** → **Deploy New Server**, then choose:
- **Type:** Cloud Compute – Shared CPU.
- **Location:** closest to your users. For South-East Asia choose **Singapore**; for US/EU judges New Jersey or Frankfurt.
- **Image:** Ubuntu **24.04 LTS x64**.
- **Plan:** the smallest **1 vCPU / 1 GB RAM** (about USD 5/month). If it is not offered, or the build runs out of memory, choose **1 vCPU / 2 GB** (about USD 10/month). Prices not re-verified — check Vultr's page.
- **SSH Keys:** tick `botlab`.
- **Hostname:** `botlab-auditor`.
- Turn off *Auto Backups* (this is a demo).

Click **Deploy**, wait 1–2 minutes until it is *Running*, then copy the **IP Address** (e.g. `45.77.12.34`).

## Step 3: SSH into the server

```powershell
PS> ssh -i $env:USERPROFILE\.ssh\vultr_botlab root@45.77.12.34
```
(Use your IP.) The first time, answer `yes` to `Are you sure you want to continue connecting`. When you see `root@botlab-auditor:~#` you are on the server.

## Step 4: Get the code onto the server

**Option A: public GitHub repo** (branch `vultr`):
```bash
# apt-get update && apt-get install -y git
# git clone -b vultr https://github.com/botlabtrade/botlab-bot-log-auditor.git /opt/botlab-auditor
```

**Option B: copy from your machine.** Open a **new PowerShell** on your machine (keep the SSH window open):
```powershell
PS> cd C:\path\to\parent-folder
PS> tar --exclude=hackathon_auditor/worker/node_modules --exclude=hackathon_auditor/video --exclude=hackathon_auditor/.git -czf auditor.tgz hackathon_auditor
PS> scp -i $env:USERPROFILE\.ssh\vultr_botlab auditor.tgz root@45.77.12.34:/root/
```
Back in the SSH window:
```bash
# mkdir -p /opt && tar -xzf /root/auditor.tgz -C /opt && mv /opt/hackathon_auditor /opt/botlab-auditor
```

## Step 5: Install Docker and prepare (automated)

```bash
# bash /opt/botlab-auditor/vultr/deploy.sh install
```
The script installs official Docker, opens the firewall for ports 22/80/443, adds 1 GB swap and creates `vultr/.env`.
(With Option A you can run `bash deploy.sh install https://github.com/botlabtrade/botlab-bot-log-auditor.git` to let the script clone.)

## Step 6: Fill in `.env` (YOU type the key)

```bash
# nano /opt/botlab-auditor/vultr/.env
```
Edit these lines (arrow keys to move):
- `DOMAIN=`: leave **empty** without a domain. With a domain write `auditor.example.com`, and create a DNS A record pointing to the server IP **first**.
- `PUBLIC_BASE_URL=`: `http://45.77.12.34` (or `https://auditor.example.com`).
- For the real AI (Vultr Serverless Inference):
  - `LLM_PROVIDER=openai_compatible`
  - `LLM_API_KEY=`: **paste your Inference API key here** (right-click pastes in the SSH window).
  - `LLM_MODEL=`: the model id. List them with Step 6b.
- For a token-free demo keep `LLM_PROVIDER=mock`.

Save with **Ctrl+O**, Enter, exit with **Ctrl+X**.

**6b. List available models** (the key is read from the file, never shown on screen):
```bash
# set -a; . /opt/botlab-auditor/vultr/.env; set +a
# curl -s https://api.vultrinference.com/v1/models -H "Authorization: Bearer $LLM_API_KEY" | python3 -m json.tool | grep '"id"'
```
Pick a chat/instruct model and copy its exact `id` into `LLM_MODEL=` in `.env`.

## Step 7: Run

```bash
# bash /opt/botlab-auditor/vultr/deploy.sh up
```
The first build takes a few minutes. At the end the script prints the URL and `{"ok": true, "sandbox": "runner container (unix)"}`.

## Step 8: Check

1. Open `http://<IP>` (or `https://<domain>`).
2. The top-right badge must show **LLM: Vultr Serverless Inference** (or `mock (offline)` if you chose mock).
3. Click a sample (e.g. *bybit closed pnl*), set *Risk per trade* to `20`, click **Run audit**.
4. In **Agent steps** you see `↳ Step 1 · run_python ...` steps one by one, and below them the **Agent follow-up analyses** card with each step's code and output.

## Logs and status

```bash
# bash /opt/botlab-auditor/vultr/deploy.sh status     # running containers + health
# bash /opt/botlab-auditor/vultr/deploy.sh logs       # live logs, Ctrl+C to exit
```
Common errors:
- `LLM HTTP 401`: wrong key, or the account key instead of the Inference key. Fix `.env`, run `deploy.sh up` again.
- `LLM HTTP 404 / model not found`: wrong `LLM_MODEL`. See Step 6b.
- HTTPS does not load: DNS not pointing at the IP yet, or not propagated. Try `DOMAIN=` empty and `http://<IP>` first.
- New code: Option A `bash deploy.sh update`; Option B copy the tgz again and run `deploy.sh up`.

## Shut down to save credit

- **Pause the app** (no AI calls): `bash /opt/botlab-auditor/vultr/deploy.sh stop`. The server is **still billed hourly**.
- **Stop billing completely:** in Vultr open the server → **Server Destroy** (trash icon). On Vultr a *stopped* server is still billed; only *Destroy* stops it. Destroy after judging.
- **Inference:** delete the subscription under *Serverless → Inference* when no longer needed.

## Built-in security

- Model-written code does **not** run in the main app but in a separate `runner` container: **no network** (`network none`), read-only root filesystem, `/tmp` in RAM, non-root user, memory/CPU/process limits, Docker's default seccomp, and no keys inside.
- Only Caddy is exposed to the Internet (ports 80/443). `.env` has mode `600` (root only).
- Educational tool; sample data is synthetic. **Not financial advice.**
