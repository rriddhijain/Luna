# 🧠 SAMANVAY — CV/ML Matching Module Documentation

This guide explains the matching framework implemented for the **CV/ML Engineer (Seat ①)**. The code is structured to work independently, with built-in fallbacks so it doesn't break if other seats are still developing.

---

## 📂 Directory Layout

All matching code resides in the `samanvay/match/` directory:

```
samanvay/match/
├── __init__.py      # Package indicator
├── classical.py     # Main matching coordinator (SIFT, ORB, L2)
├── detect.py        # Keypoint detector (FAST, SIFT, PC maxima)
├── describe.py      # Feature descriptor extraction
└── tile.py          # Tiled matching and cell budget manager
```

---

## 🛠️ How it Works (File by File)

### 1. Keypoint Detection ([`detect.py`](../samanvay/match/detect.py))
Responsible for finding points of interest (features) in the image:
* **SIFT/ORB**: Uses standard OpenCV methods to find corners and edges.
* **L2**: Searches for peak intensity coordinates (local maxima) in the **Phase Congruency map** (which is illumination-robust).
* **💡 Independent Fallback**: If the phase congruency map is not populated (all zeros), it automatically calculates standard image gradient magnitudes (Sobel edges) and detects local maxima there.

### 2. Feature Description ([`describe.py`](../samanvay/match/describe.py))
Responsible for generating a mathematical representation (descriptor vector) for each point, so they can be matched:
* **SIFT/ORB**: Extracts OpenCV descriptors. Handles ORB-specific formatting (`uint8` Hamming distance).
* **L2**: Computes a local **Histogram of Orientations** (128 dimensions) from the dominant orientation maps.
* **💡 Independent Fallback**: If the orientation map is empty, it automatically extracts gradient orientations (`arctan2`) of the raw image on the fly.

### 3. Match Coordinator ([`classical.py`](../samanvay/match/classical.py))
This file orchestrates the matching process:
1. Calls `detect.py` to get keypoints on both images.
2. Calls `describe.py` to get descriptors.
3. Matches the descriptors using a Brute-Force Matcher (`BFMatcher`).
4. Runs a **ratio test** (`0.75` for SIFT, `0.9` for L2) to filter out bad matches. If the distance is exactly `0` (identical feature matches), it automatically accepts it.

### 4. Tiled Matching & Budgets ([`tile.py`](../samanvay/match/tile.py))
Divides images into grid cells (e.g. 4x4) to ensure features are distributed uniformly across the entire image:
* **Overlap Halo**: Adds a small boundary margin (64px) to each tile so matches near the edges aren't missed, and then removes duplicate matches at borders.
* **Bidirectional Quotas (I6 Design)**:
  * **Underpopulated Cells**: If a cell gets fewer matches than the minimum budget, the code runs a loop that relaxes the ratio threshold incrementally (e.g. `0.75 -> 0.80 -> 0.85 -> 0.90`) and retries matching to harvest more points.
  * **Overpopulated Cells**: If a cell gets more matches than the maximum budget, it sorts them by matching score and caps the output to the top-K matches.

---

## 🏃 Running and testing matching methods

You can choose the matcher method dynamically in your configuration dictionary:
```python
config = {
    "match": {
        "method": "sift",             # Options: "sift", "orb", "l2"
        "ratio_threshold": 0.75       # Matching filter threshold
    }
}
```

To run the automated test suite verifying all matchers:
```bash
pytest tests/test_matching.py
```
