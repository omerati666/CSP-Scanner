# CSP Scanner

Her gün hafta içi otomatik çalışıp **cash secured put (CSP) adaylarını** tarayan ve sonucu mail atan sistem.
Veri: MarketData.app · Çalıştırma: GitHub Actions (kendi bilgisayarının açık olması gerekmez).

Kriterler `config.yaml` dosyasındadır (kaynak: pia-shadow reposundaki `CSP2_SPEC_V1`, yani Barchart ayarların).

---

## KURULUM (bir kez yapılır, ~20 dakika)

### 1) GitHub'da yeni (private) repo aç
1. github.com → sağ üstte **+** → **New repository**
2. Repository name: `csp-scanner`
3. **Private** seçili olsun. (Public yapma.)
4. **Create repository**

### 2) Dosyaları yükle
1. Bu ZIP'i bilgisayarında çıkar.
2. Yeni reponun sayfasında **uploading an existing file** bağlantısına tıkla.
3. Çıkardığın klasörün **içindeki her şeyi** (config.yaml, scanner.py, tests, data... ) sayfaya sürükle bırak.
4. Alttaki **Commit changes** düğmesine bas.

### 3) Workflow dosyasını kontrol et (en kritik adım)
`.github` klasörü gizli olduğu için sürükle-bırakta bazen yüklenmez.
Repo ana sayfasında **.github** klasörü görünüyorsa içinde `workflows/daily-scan.yml` var mı bak. Varsa bu adımı atla.

Yoksa:
1. **Add file → Create new file**
2. Dosya adı kutusuna aynen şunu yaz: `.github/workflows/daily-scan.yml`
   (Eğik çizgiyi yazınca klasörler otomatik oluşur.)
3. ZIP'teki `.github/workflows/daily-scan.yml` dosyasının içeriğini kopyalayıp yapıştır.
4. **Commit changes**

### 4) Gmail uygulama şifresi oluştur
Normal Gmail şifreni ASLA kullanma. Bunun yerine:
1. myaccount.google.com → **Security (Güvenlik)**
2. **2-Step Verification** kapalıysa aç (zorunlu).
3. Arama kutusuna **App passwords** yaz, aç.
4. Uygulama adı: `csp-scanner` → **Create**
5. Çıkan 16 haneli şifreyi kopyala (boşluklu kalabilir).

### 5) Gizli bilgileri GitHub'a ekle
Repo → **Settings → Secrets and variables → Actions → New repository secret**.
Aşağıdaki 4 secret'ı tek tek ekle (isimler aynen böyle, büyük harfle):

| Name | Value |
|---|---|
| `MARKETDATA_TOKEN` | MarketData hesabındaki API token |
| `GMAIL_USER` | Gmail adresin |
| `GMAIL_APP_PASSWORD` | 4. adımdaki 16 haneli şifre |
| `MAIL_TO` | Maillerin gideceği adres (birden fazlaysa virgülle ayır) |

> Bu bilgileri sohbete, dosyalara veya e-postaya yazma. Sadece GitHub Secrets'a gir.

### 6) Önce küçük bir test yap
1. Repo → **Actions** sekmesi (ilk seferde "I understand my workflows, go ahead and enable them" çıkarsa onayla)
2. Soldan **Daily CSP Scan** → sağda **Run workflow**
3. `symbols` kutusuna `AAPL,MSFT,NVDA` yaz → **Run workflow**
4. 1-2 dakika bekle. Yeşil tik = başarılı, mailin gelmiş olmalı.
5. Kırmızı çarpı çıkarsa o çalışmaya tıkla, hata satırını kopyalayıp bana gönder.

### 7) Tam taramayı çalıştır
Aynı yerden `symbols` kutusunu **boş bırakarak** Run workflow yap. Mail geldiyse kurulum bitti.
Bundan sonra sistem her hafta içi günü kendiliğinden çalışır.

---

## GÜNLÜK KULLANIM

**Kriter değiştirmek:** Repo'da `config.yaml` → kalem simgesi → sayıyı değiştir → **Commit changes**. Bir sonraki taramada geçerli olur.

**Hisse eklemek/çıkarmak:** Yine `config.yaml` içinde `universe:` listesi.

**Saati değiştirmek:** `.github/workflows/daily-scan.yml` içindeki `cron: "0 19 * * 1-5"` satırı. Saat **UTC**'dir, Türkiye = UTC+3.
Şu an 19:00 UTC = **22:00 Türkiye**. Örneğin 20:30 Türkiye için `"30 17 * * 1-5"` yaz.
GitHub zamanlanmış çalışmaları yoğunlukta birkaç dakika geciktirebilir.

**Çalıştırma sonrası detay:** Actions → ilgili çalışma → en altta **csp-report** (HTML rapor ve CSV).

---

## ÖNEMLİ BİLMEN GEREKENLER

1. **IV rank / IV percentile:** MarketData bu iki değeri hazır vermiyor. Scanner her gün her hissenin IV değerini `data/iv_history.csv` dosyasına kaydeder ve kendi IVR/IVP'sini hesaplar.
   İlk 30 gün "birikiyor" yazar; bu sürede bu filtre **uygulanmaz** ve adaylar uyarıyla listelenir.
   (`config.yaml` → `iv.when_insufficient_history: reject` yaparsan bu sürede hiç aday çıkmaz.)
   Barchart'ın IVR/IVP'si 52 haftalık verisiyle hesaplanır, bizimki sıfırdan birikir. Rakamlar birebir aynı olmayabilir.
2. **Getiri formülü:** Spec'te "doğrulanmamış" işaretliydi. Burada `bid / strike × 365 / DTE` kullanıldı. Farklıysa `config.yaml` → `return` bölümünden değiştirilir.
3. **Piyasa değeri filtresi** uygulanmıyor (MarketData vermiyor). Evren önceden seçilmiş büyük şirketler.
4. **Earnings tarihi bulunamazsa aday elenir** (spec'teki "tarih biliniyor olmalı" kuralı).
5. **Veri cached moddadır** (en fazla ~20 dk eski olabilir). Emir vermeden önce güncel fiyatı broker'ında kontrol et.
6. Bu bir araştırma/tarama aracıdır, yatırım tavsiyesi değildir.

## SORUN GİDERME

| Belirti | Olası neden |
|---|---|
| Actions sekmesinde "Daily CSP Scan" yok | `.github/workflows/daily-scan.yml` yüklenmemiş (3. adım) |
| `MARKETDATA_TOKEN tanımlı değil` | Secret adı yanlış yazılmış ya da eklenmemiş |
| `MarketData yetki hatası` | Token yanlış/süresi dolmuş |
| `SMTPAuthenticationError` | Gmail normal şifresi girilmiş ya da 2 adımlı doğrulama kapalı; uygulama şifresi gerekir |
| Son adımda `push` hatası | Settings → Actions → General → Workflow permissions → **Read and write permissions** seç, Save |
| Hiç aday gelmiyor | Maildeki "Filtre özeti" hangi filtrenin eleme yaptığını gösterir |
| Hafta içi mail gelmedi | Piyasa tatili olabilir (otomatik atlanır) |
