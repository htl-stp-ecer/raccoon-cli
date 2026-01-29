import asyncio

from libstp import Step
from libstp.button import is_pressed


class WaitForButton(Step):
    async def _execute_step(self, robot) -> None:
        # await wait_for_button_press() # ToDo: use the cpp implementation when available
        while is_pressed():  # Wait until button is released
            await asyncio.sleep(0.1)

        while not is_pressed():  # Wait until button is pressed
            await asyncio.sleep(0.1)


def wait_for_button() -> WaitForButton:
    return WaitForButton()
