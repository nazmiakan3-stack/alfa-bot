def get_all_prices():
    """Coingecko API üzerinden IP engeline takılmadan toplu fiyat çeker."""
    url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum,solana,binancecoin,avalanche-2,chainlink,ripple,dogecoin,cardano,polkadot&vs_currencies=usd"
    data = http_get_json(url)
    
    if data and isinstance(data, dict):
        mapping = {
            "bitcoin": "BTCUSDT",
            "ethereum": "ETHUSDT",
            "solana": "SOLUSDT",
            "binancecoin": "BNBUSDT",
            "avalanche-2": "AVAXUSDT",
            "chainlink": "LINKUSDT",
            "ripple": "XRPUSDT",
            "dogecoin": "DOGEUSDT",
            "cardano": "ADAUSDT",
            "polkadot": "DOTUSDT"
        }
        
        prices = {}
        for cg_id, symbol in mapping.items():
            if cg_id in data and "usd" in data[cg_id]:
                prices[symbol] = float(data[cg_id]["usd"])
        
        if prices:
            return prices

    return {}
