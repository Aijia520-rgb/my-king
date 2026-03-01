"""检查当前市场的时间数据"""
import aiohttp
import asyncio
import json
from datetime import datetime, timezone
import time
import math

async def check():
    async with aiohttp.ClientSession() as session:
        now = time.time()
        current_ts = math.floor(now / 300) * 300
        slug = f"btc-updown-5m-{current_ts}"
        
        print(f"当前时间戳: {now}")
        print(f"计算的时间戳: {current_ts}")
        print(f"计算的时间: {datetime.fromtimestamp(current_ts, tz=timezone.utc)}")
        print(f"查询slug: {slug}\n")
        
        url = 'https://gamma-api.polymarket.com/events'
        params = {'slug': slug}
        
        async with session.get(url, params=params) as resp:
            data = await resp.json()
        
        if data:
            event = data[0] if isinstance(data, list) else data
            markets = event.get('markets', [])
            
            print(f"Event slug: {event.get('slug')}")
            print(f"Event title: {event.get('title')}")
            print(f"Event startDate: {event.get('startDate')}")
            print(f"Event endDate: {event.get('endDate')}")
            print(f"\nMarkets: {len(markets)}")
            
            for m in markets:
                print(f"\n--- Market ---")
                print(f"Slug: {m.get('slug')}")
                print(f"startDate: {m.get('startDate')}")
                print(f"endDate: {m.get('endDate')}")
                print(f"acceptingOrders: {m.get('acceptingOrders')}")
                print(f"closed: {m.get('closed')}")
                
                # 解析时间
                end_date = m.get('endDate', '')
                start_date = m.get('startDate', '')
                
                if end_date:
                    end_time = datetime.fromisoformat(end_date.replace('Z', '+00:00')).timestamp()
                    print(f"Parsed end_time: {end_time} ({datetime.fromtimestamp(end_time, tz=timezone.utc)})")
                
                if start_date:
                    start_time = datetime.fromisoformat(start_date.replace('Z', '+00:00')).timestamp()
                    print(f"Parsed start_time: {start_time} ({datetime.fromtimestamp(start_time, tz=timezone.utc)})")
                
                # 检查是否在时间窗口内
                if end_date and start_date:
                    remaining = end_time - now
                    elapsed = now - start_time
                    print(f"Remaining: {remaining:.0f}s")
                    print(f"Elapsed: {elapsed:.0f}s")
        else:
            print("No data returned")

asyncio.run(check())
