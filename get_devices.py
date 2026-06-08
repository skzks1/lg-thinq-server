"""
기기 ID 조회 스크립트
실행: python get_devices.py
"""
import asyncio
import json
from aiohttp import ClientSession
from thinqconnect import ThinQApi

PAT       = "thinqpat_4d2ba1f6cd474b3d7e194c80897af6471e7c6f18042f11ee9128"
CLIENT_ID = "808996f2-38d2-47f7-82cb-4c34edde5965"
COUNTRY   = "KR"

async def main():
    async with ClientSession() as session:
        api = ThinQApi(
            session=session,
            access_token=PAT,
            country_code=COUNTRY,
            client_id=CLIENT_ID,
        )
        result = await api.async_get_device_list()
        print("=== 기기 목록 ===")
        print(json.dumps(result, ensure_ascii=False, indent=2))

asyncio.run(main())
