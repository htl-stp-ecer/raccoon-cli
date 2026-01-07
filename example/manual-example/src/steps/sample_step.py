from libstp.motor import Motor

class SampleStep(Step):
    pass

@dsl
def sample_step(my_arg: bool, other_arg: str, sensor: Motor) -> SampleStep:
    return SampleStep()