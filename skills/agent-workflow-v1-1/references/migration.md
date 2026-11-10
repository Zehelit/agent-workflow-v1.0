# Durum uyumluluğu ve kurtarma sınırı

v1.1 çalışma zamanı yalnızca onaylı, değişmez çalıştırma alanındaki güncel schema2 durumunu kabul eder. Bilinmeyen, bozuk ve eski durumlar operasyonel durum değiştirilmeden reddedilir. Eski yetki metinleri, arşivlenmiş görev makbuzları ve güncelliğini yitirmiş hedef gözlemleri yürütme yetkisi vermez.

Sabitlenmiş `scripts/candidate.py` girişi taşıma komutu sunmaz; çekirdek kaynakta da eski durum dönüştürücüsü veya eski yetki doğrulama dalı bulunmaz. Başka bir kurulumdan otomatik içe aktarma, bootstrap veya paket geri dönüşü yapılmaz. Onaylı paket güncellemesinden sonra yeni bir çalıştırma oluşturun; mevcut çalıştırmanın manifestini veya yardımcı eşlemesini değiştirerek doğrulamayı aşmayın.

Güncel kurtarma, kabul edilmiş çalıştırmanın doğrulanmış kontrol noktasını ve yeni, geçerli kullanıcı yetkisini kullanır. Kaynak zaman damgaları, tekrar kullanım rezervasyonları, dosya bütünlüğü ve yazıcıların durduğu gözlemi korunur. Eski durdurma veya devam metnini yeni talimat olarak kullanmayın; kurtarılan hedefi otomatik etkinleştirmeyin.

Yeni kontrol noktalarının sahibi `agent-workflow-v1-1` kimliğidir. Çekirdek nesnenin bağımsız varsayılan durum dizini `.agent-workflow-v1-1`; desteklenen sabitlenmiş girişin çalıştırma dizini `.agent-workflow-v1-1/runs/<run-id>` olur. Eski `.agent-workflow` ve `.agent-workflow-v1-0` dizinleri içe aktarılmaz. Bu alanlar hâlâ ayrılmıştır; görev dosyası veya restore hedefi olarak kullanılamaz. Güncel kontrol noktası temizliği eski sahiplik işaretlerini silmez. Doğrudan çekirdek komut satırı kullanımı reddedilir.

## Kullanıcı kurulumunu teknik 1.1 kimliğine geçirme

1. Çalışan eski worker ve yazıcıları envanterle. Başka oturumun çalışmasını izinsiz kesme; sahipli duruş ve gerçek süreç çıkışı doğrulanmadan eski kurulumu taşıma.
2. Eski kurulu dosyaları sahiplik kaydıyla doğrula ve keşfedilen skill klasörlerinin dışında hash'i doğrulanmış yedeğini koru. Dosyalar değiştirilmişse farkları önce koru.
3. Yeni paketi yeni manifest ve harici pin ile `skills/agent-workflow-v1-1` konumuna temiz kur. Farklı kimlikteki eski kurulum üzerinde `--update` kullanma.
4. Keşif yollarını değiştirmeden önce kurulu yeni girişten yeni, benzersiz bir doğrulama run kimliği ile `workflow validate` çalıştır; dönen kimlik, sürüm, çalışma alanı ve manifest pin'ini karşılaştır. Bu işlem yeni bir `run-binding.json` yazar; goal, workflow state veya watcher başlatmaz.
5. Yeni çağrı `$agent-workflow-v1-1`'dir. Doğrudan kurulum doğrulaması geçtikten sonra Codex'teki ana skill ve alt rol yolu kayıtlarını yeni klasöre taşı; mevcut enabled/disabled değerlerini ve açık çağrı politikasını koru. Config eşzamanlı değişmişse üzerine yazma. Sahipli süreçleri yeniden envanterle; başka oturumun canlı worker'ı varsa onu veya kurulumunu izinsiz kesme. Güvenli geçişte eski kurulumu skill keşif kökünün dışına arşivle ve config/yedek dosyalarını yeniden doğrula.
6. Eski checkpoint, run binding ve receipt dosyalarının içindeki kimlik, yol veya hash'leri değiştirme. Yeni paketle yeni çalışma oluştur; önceki tamamlanmamış görevin içeriği gerekiyorsa doğrulanmış çıktılarını ve gerçek güncel yetkiyi yeni çalışmaya ayrıca sağla.
