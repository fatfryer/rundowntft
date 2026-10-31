#!/usr/bin/env python3
"""
Simple ROI Labeler - Click ROIs in order, save as ordered list.
No names needed - you tell me the meaning after.
"""

import json
import cv2
import numpy as np
from pathlib import Path

ROI_FILE = Path("rois.json")


class ROILabeler:
    def __init__(self, image_path: str):
        self.image = cv2.imread(image_path)
        if self.image is None:
            raise ValueError(f"Could not load image: {image_path}")
        self.h, self.w = self.image.shape[:2]
        self.rois: list[list[tuple[float, float]]] = []  # Ordered list of ROIs (each = 4 corners)
        self.current_points: list[tuple[int, int]] = []
        self.window_name = "ROI Labeler - Click 4 corners per ROI, 'n'=next, 'r'=redo, 'b'=back, 's'=save, 'q'=quit"

    def normalize_point(self, x: int, y: int) -> tuple[float, float]:
        return (x / self.w, y / self.h)

    def denormalize_point(self, nx: float, ny: float) -> tuple[int, int]:
        return (int(nx * self.w), int(ny * self.h))

    def mouse_callback(self, event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN and len(self.current_points) < 4:
            self.current_points.append((x, y))
            self.redraw()

    def redraw(self):
        display = self.image.copy()

        # Draw completed ROIs
        for i, points in enumerate(self.rois):
            pts = np.array([self.denormalize_point(*p) for p in points], np.int32)
            cv2.polylines(display, [pts], True, (0, 255, 0), 2)
            cx = int(np.mean([p[0] for p in points]) * self.w)
            cy = int(np.mean([p[1] for p in points]) * self.h)
            cv2.putText(display, f"roi_{i}", (cx - 30, cy - 10),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)

        # Draw current points
        for i, (x, y) in enumerate(self.current_points):
            cv2.circle(display, (x, y), 5, (0, 0, 255), -1)
            cv2.putText(display, str(i+1), (x+8, y-8),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)
        if len(self.current_points) > 1:
            pts = np.array(self.current_points, np.int32)
            cv2.polylines(display, [pts], False, (0, 0, 255), 2)

        # Status
        cv2.putText(display, f"ROI #{len(self.rois)} (0-indexed)", (10, 30),
                   cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
        cv2.putText(display, f"Points: {len(self.current_points)}/4", (10, 55),
                   cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
        cv2.putText(display, f"Total ROIs saved: {len(self.rois)}", (10, 80),
                   cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)

        # List on right
        y_start = 120
        for i in range(len(self.rois)):
            cv2.putText(display, f"  roi_{i}", (self.w - 150, y_start + i * 22),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)

        cv2.imshow(self.window_name, display)

    def run(self):
        cv2.namedWindow(self.window_name, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(self.window_name, min(1700, self.w + 100), min(950, self.h))
        cv2.setMouseCallback(self.window_name, self.mouse_callback)

        # Load existing if any
        if ROI_FILE.exists():
            try:
                with open(ROI_FILE) as f:
                    data = json.load(f)
                if data.get("image_width") == self.w and data.get("image_height") == self.h:
                    for roi in data.get("rois", []):
                        self.rois.append([(p["x"], p["y"]) for p in roi])
                print(f"Loaded {len(self.rois)} existing ROIs")
            except Exception as e:
                print(f"Could not load: {e}")

        self.redraw()

        while True:
            key = cv2.waitKey(0) & 0xFF

            if key == ord('q'):
                break
            elif key == ord('n'):  # Next ROI
                if len(self.current_points) == 4:
                    self.rois.append([self.normalize_point(x, y) for x, y in self.current_points])
                    self.current_points = []
                    self.redraw()
                else:
                    print(f"Need 4 points, have {len(self.current_points)}")
            elif key == ord('r'):  # Redo current
                self.current_points = []
                self.redraw()
            elif key == ord('b'):  # Back (delete last saved)
                if self.rois:
                    self.rois.pop()
                self.current_points = []
                self.redraw()
            elif key == ord('s'):  # Save
                self.save()
                print("Saved!")
            elif key == ord('d'):  # Delete last saved (same as b)
                if self.rois:
                    self.rois.pop()
                self.current_points = []
                self.redraw()

        cv2.destroyAllWindows()

    def save(self):
        data = {
            "image_width": self.w,
            "image_height": self.h,
            "rois": []
        }
        for roi in self.rois:
            data["rois"].append([{"x": p[0], "y": p[1]} for p in roi])

        with open(ROI_FILE, 'w') as f:
            json.dump(data, f, indent=2)
        print(f"Saved {len(self.rois)} ROIs to {ROI_FILE}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python roi_labeler.py <frame_image>")
        sys.exit(1)

    labeler = ROILabeler(sys.argv[1])
    labeler.run()