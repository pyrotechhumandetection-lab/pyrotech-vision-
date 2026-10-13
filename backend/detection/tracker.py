"""ByteTrack with buffered IoU for vehicles (fast-moving vehicle fix).

ByteTrack links a detection to a track only when their boxes overlap (IoU). A fast car, truck or
2-wheeler can move further than its own width between two analysed frames, so its boxes never
overlap: the tracker keeps creating a new, never-confirmed track and the vehicle is dropped although
the model detected it in almost every frame (measured on CAM-02 frames - see README).

Buffered IoU (as in the C-BIoU tracker): for the association cost only, track and detection boxes are
enlarged by `vehicle_iou_buffer` x their width/height on each side, so consecutive positions of a fast
vehicle still overlap. It applies only to vehicle-vs-vehicle pairs (COCO bicycle, car, motorcycle,
bus, truck); persons use the unchanged ByteTrack cost. The normal IoU is kept wherever it is better
(minimum cost), so slow traffic is associated exactly as before. Boxes, scores and Kalman states are
not changed. Configured in models/bytetrack_stable.yaml (vehicle_iou_buffer: 0 disables it).
"""
import numpy as np
from ultralytics.trackers import track as _track
from ultralytics.trackers.byte_tracker import BYTETracker
from ultralytics.trackers.utils import matching

VEHICLE_COCO_IDS = {1, 2, 3, 5, 7}


def _buffered(boxes, b):
    w = boxes[:, 2] - boxes[:, 0]
    h = boxes[:, 3] - boxes[:, 1]
    return np.stack([boxes[:, 0] - b * w, boxes[:, 1] - b * h, boxes[:, 2] + b * w, boxes[:, 3] + b * h], axis=1)


class BufferedByteTracker(BYTETracker):
    def get_dists(self, tracks, detections):
        b = float(getattr(self.args, "vehicle_iou_buffer", 0) or 0)
        if b <= 0 or not tracks or not detections:
            return super().get_dists(tracks, detections)
        veh_t = np.array([int(t.cls) in VEHICLE_COCO_IDS for t in tracks])
        veh_d = np.array([int(d.cls) in VEHICLE_COCO_IDS for d in detections])
        if not (veh_t.any() and veh_d.any()):
            return super().get_dists(tracks, detections)
        dists = matching.iou_distance(tracks, detections)
        tb = _buffered(np.array([t.xyxy for t in tracks], dtype=np.float32), b)
        db = _buffered(np.array([d.xyxy for d in detections], dtype=np.float32), b)
        bdists = matching.iou_distance(list(tb), list(db))
        dists = np.where(veh_t[:, None] & veh_d[None, :], np.minimum(dists, bdists), dists)
        if self.args.fuse_score:
            dists = matching.fuse_score(dists, detections)
        return dists


def install():
    """Use the buffered tracker for tracker_type 'bytetrack' (this process only)."""
    _track.TRACKER_MAP["bytetrack"] = BufferedByteTracker
