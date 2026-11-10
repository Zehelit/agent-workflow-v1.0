# AGWF çalışma yaşam döngüsü güvenilirliği

## İstenen sonuç

İlk etkinleştirme, gerçek ölçüm yenileme, teknik kesintiden toparlanma ve doğal kapanış güvenilir olsun. Test hazırlığı hedef çalışırken üretilmesin; başlangıç başarısı, durmuş çalışma kaydından devam başarısı ve zamanlama başarısı ayrı kanıtlarla raporlansın.

## Mevcut kanıt ve sınırları

- `reports/goal-live-test/live-test-result.json`: İlk başlangıç, eskimiş ölçüm nedeniyle etkinleşmeden SAVE verdi. Zamanlama 98,74 ve 45,05 saniyeydi; iki 10 saniyelik tur da başarısız. İkinci turdaki yeni izleme dönemi ve ilk bağlama, etkinleşmiş/halted helper'ın resume edildiğini kanıtlamıyor.
- `reports/goal-normal-flow/verification.json`: Sağlıklı başlangıç, 43,423 saniye sonra ikinci gerçek ölçüm, doğrulanmış checkpoint ve doğal complete/worker çıkışı geçti. Bu koşumda yapay duruş veya SAVE kilidi yoktu.
- `scripts/candidate.py`: Teknik epoch açma ve başarısız yeniden başlatmanın kontrolünü geri koyma yolu `resume` için var; ilk `activate`/`bind-active-goal` başarısızlığı aynı kayıt ve yeniden deneme sözleşmesine sahip değil. Taze ölçümle ilk etkinleştirmeyi tekrar denemenin davranışı regresyon testiyle kanıtlanacak.
- İzin/yönerge çelişkileri ile dört test yardımcısı/bekleme hatası önceki bakımda düzeltildi. Bunlar yeniden tasarım değil, korunacak regresyonlardır.

## Gereksinimler

1. Yalnız kaydedilmiş, aynı hedef/koşum kapsamındaki `usage_receipt_expired` başlangıç hatası; yeni gerçek ölçüm, gerçek aktif hedef gözlemi ve doğrulanmış worker yokluğuyla mevcut yetki altında tekrar denenebilsin. Yeni kullanıcı mesajı, yeni run açma veya kullanım verisi değiştirme gerekmesin.
2. Gerçek kota/erken rezerv duruşu, kullanıcı duruşu, pasif hedef, bilinmeyen worker kimliği, değişmiş paket/kanıt veya farklı hedef bu yoldan aşılamasın. Python'un sağladığı şey biçim/tutarlılık kontrolüdür; kullanıcı ve araç kaynağını bağımsız doğruladığı iddia edilmesin.
3. Başarısız denemeler izlenebilir olsun; yarım başlatma etkin workflow gibi görünmesin. Yeni bir gerçek SAVE kanıtı geri alınmasın veya silinmesin.
4. Hazırlık ve paket kontrolleri gerçek ölçümden önce tamamlansın. Ölçüm gerçek araç yanıtı alınırken zamanlansın; dosya yazma zamanı eski ölçüme yeni tarih vermesin. `ingest` ile lease yenilemenin ayrı olduğu korunsun.
5. Normal akış yapay duruş üretmesin. Gerçek karar `CHECKPOINT_SOON` ise checkpoint, gerçekten `SAVE_AND_STOP` ise koruma/toparlanma kuralları işletilsin.
6. Etkin/halted workflow'dan gerçek native Continue ve teknik toparlanma, ilk bağlama başarısından ayrı kabul testleriyle kanıtlansın.
7. Zamanlama testi yalnız açık test modunda çalışsın. Önceden hazırlanmış başlatıcı, gerçek native olay zamanını esas alsın. Hedef zamanı geçtikten sonra başlatılan veya hazırlığı yetişmeyen tur zamanlama başarısı sayılmasın. Önerilen canlı SAVE tetikleme toleransı 10 ± 1 saniyedir; gerçek değer her zaman yazılsın.
8. Yerleşik goal araçları host üzerindeki ana ajan tarafından çağrılmaya devam etsin. Arka plan worker'ına uydurma host-tool erişimi, özel Codex taşıması veya kimlik bilgisi okuma yolu eklenmesin. Resmî olay kaynağı bulunmadan arayüz tıklamasından itibaren kesin zaman garantisi verilmesin.
9. Kaydetme, worker çıkışı, workflow complete ve native goal complete ayrı durumlar olarak doğrulansın. Kullanıcı açıkça istemedikçe hedef duraklatılmasın; `blocked` native aracın gerçek koşullarıyla sınırlı olsun.
10. Python 3.11+ ve standart kütüphane korunsun. Windows dosya paylaşımı hatalarında geçici kilit, süreç çıkışı veya başarı sayılmasın. Mevcut kirli/stage edilmiş çalışma ve eski kayıtlar korunarak paket güncellensin; eski run bağı sessizce yeniden yazılmasın.

## Yetki sınırı

Belgenin ilk hazırlanması yalnız planlama kapsamındaydı. Kullanıcı daha sonra planın uygulanmasını, v1.1 kaynak commit/push işlemini ve depo belgelerinin yenilenmesini açıkça yetkilendirdi; GitHub Release veya tag oluşturulmayacak. Süreli canlı kesinti testi ayrı açık tanılama seçimi gerektirir. Kaynak değişiklikleri ve izole testler bu uygulama kapsamındadır; eski koşumların paket bağları sessizce taşınmaz.
