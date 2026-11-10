# AGWF: tüm tespitlere uygulanan düzeltmeler

28 Ekim 2026. Önceki üç düzeltmeye ek olarak izleyicinin açık kalan tasarım sorunları da giderildi. AGWF ve entegre izleyici kullanıcı yeniden etkinleştirene kadar kapalıdır.

| Tespit | Uygulanan değişiklik ve doğrulama |
|---|---|
| Altı rolün otomatik seçilebilmesi | Her role ayrı `allow_implicit_invocation: false` ayarı ve hazırlanmış görev sınırı kondu. Yedi skill'in paketleme testi bu ayarı ayrı ayrı doğruluyor. |
| Kaynak, GitHub ve kurulumun farklı olması | Gerekli araçlar paket kapsamına taşındı. Temiz Git commit'inden üretilen manifest/ZIP ile yerel kurulum karşılaştırılıyor. |
| Windows okuma/yazma çakışması | Paylaşımlı okuyucu ve dosya değiştirme düzeltmeleri tüm ilgili bileşenlere taşındı. Gerçek geçici Windows dosyalarında kilit, yeniden deneme ve eski içeriği koruma testleri var. |
| Her ölçümde ayrı Codex sunucusu açılması | Bu kod kaldırıldı. Ana ajan mevcut Codex bağlantısındaki limit aracının sonucunu verir; Python yalnız bu ölçüm kaydını okur. Ölçüm yolu süreç başlatma çağrıları engellenerek sınanıyor. |
| PATH'ten farklı Codex sürümü seçilmesi | Ölçüm yolunda Codex yürütülebilir dosyası seçimi tamamen kaldırıldı; eşleştirilecek ikinci CLI kalmadı. |
| Hata çıktısının atılması | Alt Codex sunucusu kaldırıldı. Okuma hataları sabit tanı kodu, hata sınıfı ve aşamasıyla en fazla 20 kayıt olarak tutulur. Beklenmeyen Python çalışan hatalarının yerel stderr kaydı korunur. |
| İki döngünün saniyede bir paketi taraması | Sağlık bildirimi yalnız koşum/dönem kimliğini denetler. Tam dosya doğrulaması komut ve ölçüm sınırında yapılır; ölçüm döngüsündeki tek doğrulama test edilir. |
| Testlerin yalnız sahte sunucuyla geçmesi | Eski RPC sunucusu testleri yeni ölçüm kaydı sözleşmesine taşındı. Gerçek Codex limit aracı yanıtı da geçici dosyada doğrulandı. Canlı ürün kabulü, birim testinden ayrı tutuluyor. |
| Ek incelemede bulunan tutarsızlıklar | Rol politikasında kalmış `%10` durdurma kaydı gerçek `%20` eşiğiyle eşitlendi. Eksik veriyle reddedilen başlatmanın aktif işareti temizleniyor. |

Yeni veri akışı: ana ajan gerçek limit aracını çağırır, yanıtın gözlem zamanını ve kaynak kimliğini koruyarak `--usage-input` ile iletir. İzleyici bu veriyi koşum ve döneme bağlar. Eski dosyayı yeniden okumak yeni ölçüm sayılmaz. Eksik, gelecekte, süresi dolmuş, geri alınmış veya başka koşuma ait veri reddedilir. Aynı kaydı tekrar yayımlamak tarihi ilerletmez. Yenileme gelmezse izleyici doğrulamayı bir kez tekrarlar ve SAVE_AND_STOP kaydı bırakır.

Bu tasarımda ana ajan etkin çalışma sırasında ölçümü süre dolmadan yenilemekle sorumludur; arka plan Python süreci host aracını kendi başına çağıramaz. Dolayısıyla ayrı bir Codex sunucusu üzerinden sürekli veri çekildiği iddiası yoktur. `ingest` yalnız veri yayımlar; iş akışını açmaz, çalışan başlatmaz ve durdurma kaydını temizlemez. Yetki ve native hedef gözlemlerinin tazelik kuralları da bu veriyle yenilenmez.

Doğrulama: 111 hedefli test normal ve `-O` yorumlayıcılarda geçti. Test koşusunda gerçek AGWF/Codex süreç başlatmaları engellenir; yalnız paket üretimi ve salt veri araçlarının izin verilen alt süreçleri çalışabilir. Kaynak testleri, kurulu dosya özetleri, yedi skill'in kapalı kalması ve temiz Git paketinin yeniden üretimi ayrı doğrulamalardır.

Gerçek mevcut Codex limit aracı yanıtı yeni yayınlama/okuma yolundan geçti; gözlem zamanı korundu ve yeni süreç başlatılmadı. Bu, yalnız uydurma ölçümle doğrulama yapılmadığını gösterir. Geçici okuma ve test dosyaları temizlendi; hesap/kimlik bilgileri pakete alınmadı.

Canlı AGWF/Remote birlikteliği açılarak denenmedi. Kullanıcının kapalı tutma talimatı sürüyor. Tarihsel Remote hatasının kesin uygulama içi nedeni kanıtlanmış değildir; bu rapor, tespit edilen AGWF kod ve dağıtım sorunlarının düzeltilmesini kaydeder ve OpenAI istemcisindeki bütün hataların giderildiği iddiasında bulunmaz.

İç çalışma sözleşmesi: [native limit ölçümleri](../skills/agent-workflow-v1-1/references/native-usage.md). Resmî protokol arka planı: [OpenAI Docs — Codex App Server](https://learn.chatgpt.com/docs/app-server). Bu protokolü kullanmak için yeni sunucu başlatma yolu artık AGWF ölçüm kodunda bulunmaz.
