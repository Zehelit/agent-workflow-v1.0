# agent-workflow-v1.0

Codex için görev ve ajan koordinasyonu sağlayan v1.0 kaynak paketi. Teknik skill kimliği `agent-workflow-v1-0` olur. Skill yalnız açıkça seçildiğinde kullanılır; entegre limit watcher ayrı bir skill değildir.

Python 3.11+ ve standart kütüphane gerekir. Kaynak testleri: `python -X utf8 -B -m unittest discover -s skills/agent-workflow-v1-0/tests`. Paket testleri: `python -X utf8 -B -m unittest discover -s tooling/tests`. Deterministik paket: `python -X utf8 -B tooling/build-candidate.py --output <yeni-boş-klasör>`.

Kurulum ve dış manifest pini için `skills/agent-workflow-v1-0/references/coexistence.md` belgesini izleyin. Kaynak kodu, kurulu Codex skill'ini kendiliğinden güncellemez. Geçmiş değerlendirme ve kullanıcı verileri bu depoda bulunmaz.
