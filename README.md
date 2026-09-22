Peak Reversal Futures Bot (PRFB)
Sanal Kripto Vadeli İşlem Tarama ve Sinyal Botu
Versiyon 1.0 | Contabo / VPS / Termius / GitHub uyumlu
Özellikler
Tüm Binance USD-M Futures çiftlerini tarar
LONG ve SHORT sinyalleri (6 şart + ±1 mum esnekliği)
5x izole marjin sanal (paper) işlem
Her işlem: 10 USDT nominal + 15 USDT marjin rezervi
TP = 2×ATR | SL = 1×ATR
Her 1 dakikada tarama
Her 1 saatte Telegram raporu
Program her açıldığında Telegram’a “çalışıyor” mesajı
Kurulum (Contabo / VPS)
1. Sistem hazırlığı
sudo apt update && sudo apt upgrade -y
sudo apt install python3 python3-pip python3-venv git -y
2. Projeyi klonla
git clone https://github.com/KULLANICI_ADIN/PRFB_Bot.git
cd PRFB_Bot
3. Sanal ortam
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
4. Ortam değişkenleri
cp .env.example .env
nano .env
Aşağıdaki alanları doldurun:
TELEGRAM_BOT_TOKEN → BotFather’dan aldığınız token
TELEGRAM_CHAT_ID → Mesajların gideceği chat/kanal ID
5. Çalıştırma
python main.py
6. Arka planda çalıştırma (systemd önerilir)
/etc/systemd/system/prfb.service dosyası oluşturun:
[Unit]
Description=Peak Reversal Futures Bot
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/PRFB_Bot
ExecStart=/root/PRFB_Bot/venv/bin/python main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
Sonra:
sudo systemctl daemon-reload
sudo systemctl enable prfb
sudo systemctl start prfb
sudo systemctl status prfb
Logları izlemek için:
journalctl -u prfb -f
Termius ile Yönetim
Termius’ta Contabo sunucunuza SSH ile bağlanın
cd PRFB_Bot && source venv/bin/activate
python main.py veya systemctl status prfb
Klasör Yapısı
PRFB_Bot/
├── main.py                 # Ana giriş noktası
├── config.py               # Ayarlar
├── requirements.txt
├── .env.example
├── README.md
├── bot/
│   ├── exchange.py         # Binance veri katmanı
│   ├── indicators.py       # Teknik indikatörler
│   ├── signals.py          # LONG / SHORT sinyal motoru
│   ├── paper_trader.py    # Sanal işlem yöneticisi
│   ├── scanner.py          # Tarama motoru
│   └── telegram_bot.py     # Telegram bildirimleri
└── utils/
    └── logger.py
Sinyal Mantığı (Özet)
LONG (Tepe Dönüşü)
EMA(5) kırmızı mum ile dönüş
KDJ 70-110 bölgesinde aşağı
StochRSI 80-100 + aşağı kesişim
MACD tepe ve aşağı
RSI(6) → RSI(14) 75-90’da aşağı kesişim
Williams %R 0 ~ -16 ve aşağı
SHORT (Dip Dönüşü)
LONG koşullarının tam tersi.
Şartlar aynı mumda veya en fazla 1 mum önce/sonra gerçekleşse bile geçerli sayılır.
Uyarı
Bu bot sadece sanal (paper trading) amaçlıdır.
Gerçek para ile kullanmadan önce mutlaka backtest ve forward test yapın.
Yatırım tavsiyesi değildir.
Lisans
MIT License – İstediğiniz gibi kullanabilir, değiştirebilirsiniz.
