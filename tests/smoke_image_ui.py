"""Image attachment UI and routing regression tests; no agent or clipboard is contacted."""
import asyncio
import base64
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import _env
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from deck.app import DeckApp
from deck.images import Image, Prompt

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')

async def main():
    app = DeckApp()
    app.hub.serve = AsyncMock()
    app._refresh_usage_bg = AsyncMock()
    sent = []
    async def fake_run(run, prompt, use_wt, session):
        if getattr(prompt, 'images', ()):
            run.last_images = prompt.images
        sent.append((run, prompt, session))
    app.hub._run = fake_run
    async with app.run_test(size=(140, 45)) as pilot:
        run = app.hub.attach('claude', 'session', str(_env.BASE), 'images', [])
        app.show_task(run.task)
        await pilot.pause()
        img = Image.from_bytes('screenshot.png', PNG)
        with patch('deck.app.clipboard', AsyncMock(return_value=img)):
            await pilot.press('ctrl+v')
            await pilot.pause()
        assert app.pending_images == [img]
        assert app.query_one('#attachments').display
        app.query_one('#prompt').value = '@claude describe it'
        await pilot.press('enter')
        await pilot.pause()
        assert sent[-1][0] is run and sent[-1][2] == 'session'
        assert sent[-1][1] == 'describe it' and sent[-1][1].images == (img,)
        assert not app.pending_images and not app.query_one('#attachments').display
        print('PASS clipboard image reaches tagged follow-up')
        app.query_one('#prompt').value = '/image retry claude'
        await pilot.press('enter')
        await pilot.pause()
        assert app.pending_images == [img]
        print('PASS previous attachments can be restored')

        # Queued replies retain the image snapshot, even if the clipboard changes.
        run.status = 'running'
        app.pending_images = [img]
        app.query_one('#prompt').value = 'inspect this next'
        await pilot.press('enter')
        await pilot.pause()
        assert run.queued[0].images == (img,)
        run.status = 'done'
        app.hub._after_turn(run)
        await pilot.pause()
        assert sent[-1][1].images == (img,)
        print('PASS busy-run queue retains attachments')

        path = _env.BASE / 'with spaces.png'
        path.write_bytes(PNG)
        assert app.attach_pasted_paths(str(path.as_uri()))
        assert app.pending_images[0].data == PNG
        assert not app.attach_pasted_paths("don't eat ordinary pasted prose")
        assert not app.attach_pasted_paths('plain text')
        app.query_one('#prompt').value = '/image clear'
        await pilot.press('enter')
        await pilot.pause()
        assert not app.pending_images
        print('PASS image paths and clear; normal text is preserved')

        app.pending_images = [img]
        app.query_one('#prompt').value = '@agy describe it'
        await pilot.press('enter')
        await pilot.pause()
        assert app.pending_images == [img]
        assert app.query_one('#prompt').value == '@agy describe it'
        print('PASS unsupported adapter keeps prompt and attachments')

        app.query_one('#prompt').value = ''
        await pilot.press('enter')
        await pilot.pause()
        assert sent[-1][1].images == (img,) and 'attached images' in sent[-1][1]
        print('PASS image-only submission')
        app.show_empty()
        app.chosen = {'claude'}
        app.pending_images = [img]
        app.query_one('#prompt').value = '@claude start with this'
        await pilot.press('enter')
        await pilot.pause()
        assert sent[-1][1].images == (img,) and sent[-1][2] is None
        print('PASS images reach new tasks')
        app.pending_images = [img]
        app.query_one('#prompt').value = '/plan @claude inspect this'
        await pilot.press('enter')
        await pilot.pause()
        assert sent[-1][1].images == (img,) and sent[-1][0].plan
        assert not app.pending_images
        print('PASS slash-command tasks retain image attachments')

asyncio.run(main())
