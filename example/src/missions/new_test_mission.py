from libstp_helpers.api.missions import Mission
from libstp_helpers.api.steps.sequential import Sequential, seq

class NewTestMission(Mission):
    def sequence(self) -> Sequential:
        return seq([
        ])
