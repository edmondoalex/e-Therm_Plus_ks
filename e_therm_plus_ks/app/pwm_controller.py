"""
Basic PWM controller and stage mapping for e-Therm Plus KS.

This module provides:
- PWMController: simple PI controller skeleton producing PWM 0-100
- mapping from PWM to mutually exclusive stages: MIN / MED / MAX
- utility to generate relay states (booleans) ensuring interlock

This is a foundation (step B). Parameters are configurable and persisted
by the caller (ThermEngine) later.
"""
from typing import Dict, Optional
import time

class PWMController:
    def __init__(self, kp: float = 10.0, ki: float = 0.1, windup: float = 100.0,
                 min_to_med: int = 34, med_to_max: int = 67, integral_init: float = 0.0):
        """Create a simple PI controller for temperature control.

        Args:
            kp: proportional gain (scale PWM per degree)
            ki: integral gain (scale PWM per degree-second)
            windup: integral windup clamp (max absolute integral)
            min_to_med: inclusive threshold (1..min_to_med -> MIN)
            med_to_max: inclusive threshold (min_to_med+1..med_to_max -> MED)
        """
        self.kp = float(kp)
        self.ki = float(ki)
        self.windup = float(windup)
        self.min_to_med = int(min_to_med)
        self.med_to_max = int(med_to_max)

        self.integral = float(integral_init)
        self.last_time: Optional[float] = None

    def reset(self):
        self.integral = 0.0
        self.last_time = None

    def compute_pwm(self, setpoint: float, current: float, now: Optional[float] = None) -> int:
        """Compute PWM (0-100) given setpoint and current temperature.

        This is a basic PI loop. It is intentionally simple and intended as a
        starting point for more advanced control (anti-windup, derivative, etc.).

        Args:
            setpoint: desired temperature
            current: measured temperature
            now: epoch seconds, optional (used to compute dt)

        Returns:
            pwm: int in range [0,100]
        """
        if now is None:
            now = time.time()
        if self.last_time is None:
            dt = 1.0
        else:
            dt = max(1e-6, now - self.last_time)
        self.last_time = now

        error = float(setpoint) - float(current)
        # Proportional term
        p = self.kp * error
        # Integral term
        self.integral += error * dt
        # Anti-windup clamp
        if self.integral > self.windup:
            self.integral = self.windup
        elif self.integral < -self.windup:
            self.integral = -self.windup
        i = self.ki * self.integral

        raw = p + i
        # Map to 0..100
        pwm = int(round(max(0.0, min(100.0, raw))))
        return pwm

    def pwm_to_stage(self, pwm: int) -> str:
        """Map PWM value to stage name: 'OFF', 'MIN', 'MED', 'MAX'.

        Rules (defaults):
        - pwm == 0 -> OFF
        - 1..min_to_med -> MIN
        - min_to_med+1 .. med_to_max -> MED
        - med_to_max+1 .. 100 -> MAX
        """
        pwm = int(max(0, min(100, pwm)))
        if pwm == 0:
            return "OFF"
        if pwm <= self.min_to_med:
            return "MIN"
        if pwm <= self.med_to_max:
            return "MED"
        return "MAX"

    def stage_to_relays(self, stage: str) -> Dict[str, bool]:
        """Return relay booleans for given stage ensuring interlock.

        Returns a dict: {"min": bool, "med": bool, "max": bool}
        Only the relays corresponding to the requested stage are True.
        """
        s = str(stage or "").upper()
        return {
            "min": s == "MIN",
            "med": s == "MED",
            "max": s == "MAX",
        }


# Minimal self-test when run as script
if __name__ == "__main__":
    c = PWMController(kp=10.0, ki=0.5)
    # simulate current below setpoint -> positive pwm
    pwm = c.compute_pwm(22.0, 20.0, now=time.time())
    print("PWM:", pwm, "stage:", c.pwm_to_stage(pwm), "relays:", c.stage_to_relays(c.pwm_to_stage(pwm)))
