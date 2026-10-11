# Đưa trang demo "BOTLAB Bot Log Auditor" lên mạng (Cloudflare Worker)

Trang demo chạy riêng trên Cloudflare, hoàn toàn tách khỏi VPS chạy bot, nên không đụng tới khóa sàn.
Sau khi làm xong, anh sẽ có link dạng `https://botlab-auditor.<tên-tài-khoản>.workers.dev`.

## Cần chuẩn bị
- Một tài khoản Cloudflare. Gói miễn phí là đủ, và Workers AI cũng miễn phí trong hạn mức mỗi ngày.
- Máy đã cài Node.js. Kiểm tra bằng lệnh `node -v`.
- Không bắt buộc có tài khoản PayPal. Không có thì nút PayPal sandbox "sb" vẫn chạy được, chỉ dùng tiền thử.

## Các bước (mở PowerShell)

**1. Vào thư mục worker và cài công cụ** (chỉ cần làm lần đầu)
```powershell
cd C:\Users\Administrator\Desktop\BOTLAB_launch\hackathon_auditor\worker
npm install
```

**2. Chạy kiểm tra.** Kết quả phải là `pass 30` và `fail 0`.
```powershell
npm test
```

**3. Đăng nhập Cloudflare** (chỉ cần làm lần đầu). Trình duyệt sẽ mở ra, anh bấm **Allow**.
```powershell
npx wrangler login
```

**4. Đưa lên mạng**
```powershell
npx wrangler deploy
```
Lệnh này in ra link `https://botlab-auditor....workers.dev`. Đây là link "Try it out" để gửi ban giám khảo.
Nếu Cloudflare hỏi chọn tên subdomain workers.dev thì anh chọn tên nào cũng được, ví dụ `botlab`.

**5. Mở link để kiểm tra**
- Bấm một nút "Try a sample".
- Các bước agent hiện lần lượt, sau đó là bảng 6 cổng và bản báo cáo.
- Dòng nguồn báo cáo phải ghi "Workers AI". Nếu thấy chữ "template" nghĩa là AI tạm lỗi; trang vẫn chạy bằng báo cáo mẫu.

## (Không bắt buộc) Thanh toán PayPal sandbox thật phía server
Chỉ làm bước này khi anh đã có tài khoản PayPal Developer và đã tạo một sandbox REST app:
```powershell
npx wrangler secret put PAYPAL_CLIENT_ID
npx wrangler secret put PAYPAL_CLIENT_SECRET
npx wrangler deploy
```
Mỗi lệnh `secret put` sẽ hỏi giá trị. Anh dán vào rồi Enter. Khóa được lưu trên Cloudflare, không nằm trong file nào.

## Muốn tắt PayPal hoàn toàn (mở khóa giả)
1. Sửa dòng `PAYPAL_MODE = "sb"` thành `PAYPAL_MODE = "mock"` trong file `wrangler.toml`.
2. Chạy lại `npx wrangler deploy`.

## Cập nhật sau khi sửa code
```powershell
cd C:\Users\Administrator\Desktop\BOTLAB_launch\hackathon_auditor\worker
npm test
npx wrangler deploy
```

## Gỡ trang xuống
```powershell
npx wrangler delete
```

Lưu ý:
- Gói miễn phí giới hạn CPU khoảng 10 ms cho mỗi lượt. File mẫu nhỏ chạy bình thường. File rất lớn (hàng chục nghìn lệnh) có thể báo lỗi CPU; khi đó cần gói Workers Paid.
- Đây là công cụ giáo dục, không phải lời khuyên tài chính.
