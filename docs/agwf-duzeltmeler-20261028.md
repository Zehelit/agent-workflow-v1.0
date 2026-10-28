# AGWF düzeltmeleri — 28 Ekim 2026

Altı rol skill'inin her birine kendi `agents/openai.yaml` dosyası ve `allow_implicit_invocation: false` ayarı eklendi. Rol açıklamaları hazırlanmış AGWF görevleriyle sınırlandı. Görev ve gerçek model/effort kanıtı olmadan rol çalışması durur; rol skill'leri iş akışını veya izleyiciyi başlatmaz. Ana skill'deki eski proje seçiminin hâlen geçerli olduğunu ileri süren cümle kaldırıldı.

Windows durum dosyaları için paylaşımlı okuma, izinleri koruyan dosya değiştirme ve sınırlı yeniden deneme düzeltmeleri kaynak ve kurulumda eşitlendi. `candidate.py` değişken durum okumalarında aynı paylaşımlı okuyucuyu kullanır. Kilitlenen harici okuyucu durumunda eski dosya korunur; kalıcı kilit başarı gibi gösterilmez.

Kurulumun proje klasörüne bağımlı olmaması için gereken bağlam/kullanım araçları `tools/` altında pakete ve manifest kapsamına alındı. Kaynak, paket ve kurulum aynı 41 dosyayı kullanır. Bağlam aracının eski depo kökü kopyası kaldırıldı; ilgili testlerin yolları güncellendi.

Doğrulama: 63 paket/araç testi ve dokuz Windows dosya erişimi testi geçti. Dokuz Windows testi `-O` yorumlayıcıyla ve kurulu dosyalarla ayrıca geçti. Yedi skill'in biçim doğrulaması ve dosya özetleri kontrol edildi. Paket testinde eksik altı rol politikası düzeltme öncesinde başarısız, düzeltmeden sonra başarılı oldu.

Bu bakım AGWF etkinleştirilmeden yapıldı. Yerel yapılandırmadaki yedi skill kapalı kaldı; iş akışı komutu, ajan veya izleyici başlatılmadı. Geçici test dosyalarında çekirdek dosya erişimi işlevleri çağrıldı. Canlı Remote uyumluluğu bu testlerin kapsamı değildir.

Her limit ölçümünde ayrı Codex sunucusu başlatma, PATH'ten CLI seçme, alt sunucunun hata çıktısını atma ve tekrarlı paket doğrulaması eski sürümden gelen tasarım riskleridir. Bu bakım bunları yeniden tasarlamaz ve Remote hatasının kesin nedeninin giderildiği iddiasında bulunmaz. AGWF'yi yeniden etkinleştirmek ayrı kullanıcı kararıdır.

Sonraki tamamlayıcı bakımda bu açık maddeler de ele alındı: [tüm düzeltmelerin sonucu](agwf-tum-duzeltmeler-20261028.md). Yukarıdaki paragraf ilk bakımın kapsamını kaydeder; güncel izleyici artık ek Codex sunucusu başlatmaz.
