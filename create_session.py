"""Run this script locally to create a Telegram session file.
Then upload the .session file via the web panel at http://83.217.221.162/settings/telegram
"""
import asyncio
from telethon import TelegramClient
from telethon.tl.functions.auth import SendCodeRequest
from telethon.tl.types import CodeSettings

API_ID = 35301230
API_HASH = "d41d40b44fe7797164dee2312b3770e2"
SESSION_NAME = "tenant_1"
PHONE = "+79964099682"


async def main():
    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)

    print("Connecting to Telegram...")
    await client.connect()

    print("Sending code request...")
    result = await client(SendCodeRequest(
        phone_number=PHONE,
        api_id=API_ID,
        api_hash=API_HASH,
        settings=CodeSettings()
    ))

    code = input(f"Enter code sent to {PHONE}: ")

    await client.sign_in(PHONE, code, phone_code_hash=result.phone_code_hash)
    print("Authorized!")

    me = await client.get_me()
    print(f"Logged in as: {me.first_name} ({me.phone})")

    await client.disconnect()
    print(f"\nSession file saved: {SESSION_NAME}.session")
    print("Upload this file via the web panel at http://83.217.221.162/settings/telegram")


if __name__ == "__main__":
    asyncio.run(main())
