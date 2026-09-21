import { useEffect, useRef } from "react";
import type { LiveSessionStatus } from "../../lib/live-audio-client";

interface GeminiAuroraCanvasProps {
  energy: number;
  status: LiveSessionStatus;
}

/**
 * Renders the Gemini Live bottom audio-reactive gradient aurora cloud.
 * - Hidden (`opacity: 0`) when disconnected (`status === "idle"` or `"connecting"`).
 * - Uses ResizeObserver + subpixel-safe logical dimensions so there is never a 1px seam
 *   on the right or bottom edge of the stage container.
 */
export function GeminiAuroraCanvas({ energy, status }: GeminiAuroraCanvasProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const energyRef = useRef<number>(energy);
  const statusRef = useRef<LiveSessionStatus>(status);

  useEffect(() => {
    energyRef.current = energy;
  }, [energy]);

  useEffect(() => {
    statusRef.current = status;
  }, [status]);

  const isConnected =
    status === "listening" || status === "speaking" || status === "held";

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    let animId = 0;
    let phase = 0;
    let smoothedEnergy = 0;
    let logicalW = 0;
    let logicalH = 0;

    const updateSize = () => {
      const rect = canvas.getBoundingClientRect();
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      logicalW = Math.ceil(rect.width);
      logicalH = Math.ceil(rect.height);
      canvas.width = logicalW * dpr;
      canvas.height = logicalH * dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };

    updateSize();
    const ro = new ResizeObserver(updateSize);
    ro.observe(canvas);
    window.addEventListener("resize", updateSize);

    const render = () => {
      const w = logicalW;
      const h = logicalH;
      ctx.clearRect(0, 0, w, h);

      if (statusRef.current === "idle" || statusRef.current === "connecting") {
        smoothedEnergy = 0;
        animId = requestAnimationFrame(render);
        return;
      }

      const rawE = statusRef.current === "held" ? 0 : energyRef.current;
      smoothedEnergy = smoothedEnergy * 0.88 + rawE * 0.12;

      // Advance lateral drift when there is voice input/output energy
      phase += 0.002 + smoothedEnergy * 0.018;

      // Soft radial plumes floating near the bottom of the stage (no hard rectangle edges)
      const lift = smoothedEnergy * h * 0.14;
      const radiusScale = 1 + smoothedEnergy * 0.45;
      const baseAlpha =
        statusRef.current === "held" ? 0.2 : 0.48 + smoothedEnergy * 0.42;

      const blobs = [
        {
          x: w * 0.5,
          y: h * 0.96 - lift * 0.6,
          rx: w * 0.78,
          ry: h * (0.24 + smoothedEnergy * 0.16),
          colorInner: `rgba(29, 78, 216, ${baseAlpha * 0.72})`,
          colorMid: `rgba(30, 58, 138, ${baseAlpha * 0.32})`,
        },
        {
          x: w * (0.28 + Math.sin(phase) * 0.06),
          y: h * 0.91 - lift * 0.85,
          rx: w * 0.46 * radiusScale,
          ry: h * 0.22 * radiusScale,
          colorInner: `rgba(37, 99, 235, ${(0.42 + smoothedEnergy * 0.45) * baseAlpha})`,
          colorMid: `rgba(29, 78, 216, ${(0.18 + smoothedEnergy * 0.22) * baseAlpha})`,
        },
        {
          x: w * (0.72 + Math.cos(phase * 0.85) * 0.06),
          y: h * 0.89 - lift,
          rx: w * 0.5 * radiusScale,
          ry: h * 0.24 * radiusScale,
          colorInner: `rgba(56, 189, 248, ${(0.38 + smoothedEnergy * 0.52) * baseAlpha})`,
          colorMid: `rgba(59, 130, 246, ${(0.2 + smoothedEnergy * 0.25) * baseAlpha})`,
        },
        {
          x: w * 0.5,
          y: h * 0.93 - lift * 1.05,
          rx: w * 0.38 * radiusScale,
          ry: h * 0.18 * radiusScale,
          colorInner: `rgba(186, 230, 253, ${(0.3 + smoothedEnergy * 0.55) * baseAlpha})`,
          colorMid: `rgba(96, 165, 250, ${(0.16 + smoothedEnergy * 0.28) * baseAlpha})`,
        },
      ];

      ctx.save();
      ctx.globalCompositeOperation = "screen";
      for (const b of blobs) {
        ctx.save();
        ctx.translate(b.x, b.y);
        ctx.scale(1, b.ry / b.rx);
        const g = ctx.createRadialGradient(0, 0, b.rx * 0.04, 0, 0, b.rx);
        g.addColorStop(0, b.colorInner);
        g.addColorStop(0.52, b.colorMid);
        g.addColorStop(1, "rgba(15, 23, 42, 0)");
        ctx.fillStyle = g;
        ctx.beginPath();
        ctx.arc(0, 0, b.rx, 0, Math.PI * 2);
        ctx.fill();
        ctx.restore();
      }
      ctx.restore();

      animId = requestAnimationFrame(render);
    };

    animId = requestAnimationFrame(render);
    return () => {
      cancelAnimationFrame(animId);
      ro.disconnect();
      window.removeEventListener("resize", updateSize);
    };
  }, []);

  return (
    <canvas
      ref={canvasRef}
      className="gemini-aurora-canvas"
      style={{
        opacity: isConnected ? 1 : 0,
        transition: "opacity 0.55s ease",
      }}
    />
  );
}
