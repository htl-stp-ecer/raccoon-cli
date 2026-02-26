from libstp import Step, GenericRobot
import libstp.foundation as logging


class LoggingStep(Step):
    async def _execute_step(self, robot: "GenericRobot") -> None:
        pass
        #logging.set_global_level(logging.Level.trace)
        #logging.set_file_level("LcmReader.cpp", logging.Level.info)
        logging.set_file_level("turn_motion.cpp", logging.Level.trace)
        # logging.set_file_level("fused_odometry.cpp", logging.Level.info)
        # logging.set_file_level("drive.cpp", logging.Level.trace)
        # logging.set_file_level("motor_adapter.cpp", logging.Level.trace)
        #logging.set_file_level("line_follow.py", logging.Level.trace)
        #logging.set_file_level("base.py", logging.Level.trace)
