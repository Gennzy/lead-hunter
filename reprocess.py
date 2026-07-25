import asyncio
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import select, update
from app.models import async_session, Lead
from app.analyzer import analyze_message

async def reprocess():
    async with async_session() as session:
        result = await session.execute(select(Lead).where(Lead.status == 'new'))
        leads = result.scalars().all()
        print(f"Reprocessing {len(leads)} leads...")
        
        count = 0
        for lead in leads:
            msg = lead.message_text
            user = lead.username or "Неизвестный"
            chat = lead.chat_title or "Unknown"
            
            # Get reply_to_text for context
            reply_to = None
            if lead.reply_to_id:
                reply_lead = (await session.execute(
                    select(Lead).where(Lead.message_id == lead.reply_to_id, Lead.chat_title == lead.chat_title)
                )).scalar_one_or_none()
                if reply_lead:
                    reply_to = reply_lead.message_text

            chat = lead.chat_title or "Unknown"
            result = await analyze_message(msg, chat, reply_to_text=reply_to)
            
            if result.get('is_lead'):
                # Update score and message
                lead.lead_score = result['lead_score']
                lead.recommended_message = result['recommended_message']
                count += 1
                print(f"  #{lead.id}: {result['lead_score']:.0f} — {result['recommended_message'][:50]}...")
            else:
                # Filter out non-leads
                lead.status = 'filtered'
                print(f"  #{lead.id}: FILTERED — {result.get('reason', 'no reason')}")
            
            await session.flush()
        
        await session.commit()
        print(f"\nDone: {count} updated, {len(leads) - count} filtered")

if __name__ == '__main__':
    asyncio.run(reprocess())
