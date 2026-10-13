# Đưa BOTLAB Bot Log Auditor lên Vultr (hướng dẫn cho người không chuyên)

Mất khoảng 30–45 phút. Bạn cần: tài khoản Vultr có credit, và (nếu muốn dùng AI thật) một **Inference API key** của Vultr.
Mọi lệnh dưới đây đều **copy-paste** được. Có hai nơi gõ lệnh:
- **Máy bạn (Windows PowerShell)**: dòng bắt đầu bằng `PS>`. Đừng gõ chữ `PS>`.
- **Máy chủ Vultr (sau khi SSH vào)**: dòng bắt đầu bằng `#`. Đừng gõ dấu `#`.

> **Quy tắc an toàn:** API key chỉ được gõ vào file `.env` trên máy chủ. **Không bao giờ dán key vào chat**, email, ảnh chụp màn hình hay GitHub.

---

## Bước 0: Chuẩn bị (làm một lần)

1. Đăng nhập https://my.vultr.com và kiểm tra credit của hackathon đã vào tài khoản chưa (mục *Billing*).
2. (Nếu dùng AI thật) Vào **Products → Serverless → Inference**, bấm tạo subscription, mở nó ra và chép **API Key** vào một chỗ an toàn (ví dụ trình quản lý mật khẩu). Đây là key *Inference*, không phải key tài khoản.
3. (Tuỳ chọn) Nếu có tên miền (ví dụ `auditor.tenmien.com`), lát nữa bạn sẽ trỏ nó về máy chủ để có HTTPS. Không có tên miền thì vẫn chạy được bằng `http://<IP>`.

## Bước 1: Tạo khoá SSH trên Windows (làm một lần)

Mở **PowerShell** (bấm Start, gõ `powershell`, Enter):
```powershell
PS> ssh-keygen -t ed25519 -f $env:USERPROFILE\.ssh\vultr_botlab
```
Hệ thống hỏi passphrase thì có thể Enter hai lần để bỏ trống. Sau đó in khoá **công khai** ra (khoá này chia sẻ được):
```powershell
PS> Get-Content $env:USERPROFILE\.ssh\vultr_botlab.pub
```
Copy cả dòng bắt đầu bằng `ssh-ed25519 ...`. Trên Vultr vào **Account → SSH Keys → Add SSH Key**, dán vào và đặt tên `botlab`.

## Bước 2: Tạo máy chủ (Cloud Compute)

Trên Vultr bấm **Deploy +** → **Deploy New Server**, rồi chọn:
- **Type:** Cloud Compute – Shared CPU.
- **Location:** gần người dùng nhất. Người xem ở Việt Nam/Đông Nam Á chọn **Singapore**. Ban giám khảo ở Mỹ/Âu thì có thể chọn New Jersey hoặc Frankfurt.
- **Image:** Ubuntu **24.04 LTS x64**.
- **Plan:** gói nhỏ nhất **1 vCPU / 1 GB RAM** (khoảng 5 USD/tháng). Nếu không có gói này, hoặc lúc build bị báo thiếu bộ nhớ, chọn **1 vCPU / 2 GB** (khoảng 10 USD/tháng). Giá ghi ở đây chưa kiểm lại, xem số chính xác trên trang Vultr.
- **SSH Keys:** tick khoá `botlab`.
- **Hostname:** `botlab-auditor`.
- Tắt *Auto Backups* để tiết kiệm (đây là bản demo).

Bấm **Deploy**, chờ 1–2 phút tới khi trạng thái là *Running*, rồi chép **IP Address** (ví dụ `45.77.12.34`).

## Bước 3: SSH vào máy chủ

```powershell
PS> ssh -i $env:USERPROFILE\.ssh\vultr_botlab root@45.77.12.34
```
(Thay bằng IP của bạn.) Lần đầu máy hỏi `Are you sure you want to continue connecting` thì gõ `yes`. Khi thấy dòng `root@botlab-auditor:~#` là bạn đã vào máy chủ.

## Bước 4: Đưa code lên máy chủ

**Cách A: repo GitHub công khai** (sau khi bạn đã tự push repo lên GitHub):
```bash
# apt-get update && apt-get install -y git
# git clone https://github.com/<ten-ban>/<ten-repo>.git /opt/botlab-auditor
```

**Cách B: chưa có GitHub, copy thẳng từ máy bạn.** Mở một **PowerShell mới** trên máy bạn (đừng tắt cửa sổ SSH):
```powershell
PS> cd C:\Users\Administrator\Desktop\BOTLAB_launch
PS> tar --exclude=hackathon_auditor/worker/node_modules --exclude=hackathon_auditor/video --exclude=hackathon_auditor/.git -czf auditor.tgz hackathon_auditor
PS> scp -i $env:USERPROFILE\.ssh\vultr_botlab auditor.tgz root@45.77.12.34:/root/
```
Rồi quay lại cửa sổ SSH:
```bash
# mkdir -p /opt && tar -xzf /root/auditor.tgz -C /opt && mv /opt/hackathon_auditor /opt/botlab-auditor
```

## Bước 5: Cài Docker và chuẩn bị (tự động)

```bash
# bash /opt/botlab-auditor/vultr/deploy.sh install
```
Script sẽ cài Docker bản chính thức, mở tường lửa cho cổng 22/80/443, thêm 1 GB swap và tạo file `vultr/.env`.
(Nếu dùng Cách A, có thể chạy `bash deploy.sh install https://github.com/<ten-ban>/<ten-repo>.git` để script tự clone.)

## Bước 6: Điền file `.env` (chính BẠN gõ key)

```bash
# nano /opt/botlab-auditor/vultr/.env
```
Sửa các dòng sau (dùng phím mũi tên để di chuyển):
- `DOMAIN=`: để **trống** nếu chưa có tên miền. Có tên miền thì ghi `auditor.tenmien.com`, và **trước đó** phải tạo bản ghi DNS loại A trỏ về IP máy chủ.
- `PUBLIC_BASE_URL=`: `http://45.77.12.34` (hoặc `https://auditor.tenmien.com`).
- Muốn AI thật (Vultr Serverless Inference):
  - `LLM_PROVIDER=openai_compatible`
  - `LLM_API_KEY=`: **dán Inference API key của bạn vào đây** (chuột phải để dán trong cửa sổ SSH).
  - `LLM_MODEL=`: tên model. Xem danh sách bằng lệnh ở Bước 6b.
- Muốn chạy demo không tốn token thì để `LLM_PROVIDER=mock`.

Lưu bằng **Ctrl+O**, Enter, rồi thoát bằng **Ctrl+X**.

**6b. Xem các model có sẵn** (key được đọc từ file, không hiện ra màn hình):
```bash
# set -a; . /opt/botlab-auditor/vultr/.env; set +a
# curl -s https://api.vultrinference.com/v1/models -H "Authorization: Bearer $LLM_API_KEY" | python3 -m json.tool | grep '"id"'
```
Chọn một model chat/instruct (ví dụ dòng có `kimi`, `qwen` hoặc `glm`), chép đúng `id` vào `LLM_MODEL=` trong `.env`.

## Bước 7: Chạy

```bash
# bash /opt/botlab-auditor/vultr/deploy.sh up
```
Lần đầu sẽ build trong vài phút. Cuối cùng script in ra địa chỉ để mở và dòng `{"ok": true, "sandbox": "runner container (unix)"}`.

## Bước 8: Kiểm tra

1. Mở trình duyệt vào `http://<IP>` (hoặc `https://<tên miền>`).
2. Góc trên phải phải hiện **LLM: Vultr Serverless Inference** (hoặc `mock (offline)` nếu bạn chọn mock).
3. Bấm một file mẫu (ví dụ *bybit closed pnl*), nhập *Risk per trade* là `20`, rồi bấm **Run audit**.
4. Ở khung **Agent steps** sẽ thấy các bước `↳ Step 1 · run_python ...` chạy lần lượt, và bên dưới có thẻ **Agent follow-up analyses** kèm code và kết quả của từng bước.

## Xem log, xem trạng thái

```bash
# bash /opt/botlab-auditor/vultr/deploy.sh status     # các container đang chạy + sức khoẻ
# bash /opt/botlab-auditor/vultr/deploy.sh logs       # xem log trực tiếp, Ctrl+C để thoát
```
Một số lỗi hay gặp:
- Báo `LLM HTTP 401`: key sai hoặc là key tài khoản thay vì key Inference. Sửa `.env` rồi chạy lại `deploy.sh up`.
- Báo `LLM HTTP 404 / model not found`: `LLM_MODEL` sai tên. Xem lại Bước 6b.
- Không vào được HTTPS: DNS chưa trỏ đúng IP, hoặc chưa chờ đủ vài phút. Thử để `DOMAIN=` trống và dùng `http://<IP>` trước.
- Cập nhật code mới: với Cách A chạy `bash deploy.sh update`. Với Cách B, copy lại file tgz rồi chạy `deploy.sh up`.

## Tắt để tiết kiệm credit

- **Tạm dừng ứng dụng** (không gọi AI nữa): `bash /opt/botlab-auditor/vultr/deploy.sh stop`. Máy chủ **vẫn bị tính tiền theo giờ**.
- **Ngừng tính tiền hẳn:** trên Vultr mở máy chủ → **Server Destroy** (biểu tượng thùng rác). Lưu ý: ở Vultr, *Stop* máy chủ vẫn bị tính tiền, chỉ *Destroy* mới ngừng. Hãy destroy sau khi ban giám khảo chấm xong.
- **Inference:** nếu không dùng nữa thì xoá subscription ở *Serverless → Inference* để không bị tính phí tháng sau.

## Bảo mật đã có sẵn

- Code do AI viết **không chạy trong ứng dụng chính** mà chạy trong container `runner` riêng: **không có mạng** (`network none`), ổ hệ thống chỉ-đọc, `/tmp` tạm trong RAM, user không phải root, bị giới hạn RAM/CPU/số tiến trình, dùng seccomp mặc định của Docker và không chứa bất kỳ key nào.
- Ứng dụng chỉ mở ra Internet qua Caddy (cổng 80/443). File `.env` để quyền `600` (chỉ root đọc được).
- Đây là công cụ giáo dục, dữ liệu mẫu là dữ liệu tổng hợp. **Không phải lời khuyên tài chính.**
