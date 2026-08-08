"""Script to check and clear webhook for the bot."""
import asyncio
from telegram import Bot
from config import Config

async def check_and_clear_webhook():
    """Check if webhook is set and clear it."""
    bot = Bot(token=Config.TELEGRAM_BOT_TOKEN)
    
    try:
        # Get webhook info
        webhook_info = await bot.get_webhook_info()
        
        print("="*60)
        print("WEBHOOK INFORMATION")
        print("="*60)
        print(f"URL: {webhook_info.url}")
        print(f"Has custom certificate: {webhook_info.has_custom_certificate}")
        print(f"Pending update count: {webhook_info.pending_update_count}")
        print(f"Is set: {webhook_info.url != ''}")
        print("="*60)
        
        if webhook_info.url:
            print("\nWARNING: Webhook is SET - this conflicts with polling!")
            print("Clearing webhook...")
            result = await bot.delete_webhook(drop_pending_updates=True)
            if result:
                print("SUCCESS: Webhook cleared successfully!")
            else:
                print("ERROR: Failed to clear webhook")
        else:
            print("\nOK: No webhook set - polling should work")
            
    except Exception as e:
        print(f"❌ Error: {e}")
    finally:
        await bot.close()

if __name__ == "__main__":
    try:
        Config.validate()
        asyncio.run(check_and_clear_webhook())
    except ValueError as e:
        print(f"Configuration error: {e}")

