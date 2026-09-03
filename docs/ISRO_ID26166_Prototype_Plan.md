# ISRO Hackathon ID26166: Master Prototype Plan & Winning Strategy

This document serves as the master blueprint for developing the prototype. It is divided into two main sections: **The Competitive Differentiators** (the specific implementations that will separate this solution from the rest) and **The Complete System Blueprint** (the end-to-end architecture required to build a fully functional, ideal solution).

---

## PART 1: The Competitive Differentiators (How to Win)

Building a basic deep learning pipeline (like standard LoFTR or SIFT) is table stakes. To secure first place, the prototype must explicitly solve the specific edge cases of lunar imagery where standard pipelines fail. 

### 1. Illumination & Shadow Inversion Invariance
*   **The Problem:** Lunar craters illuminated from opposite sun angles cast inverse shadows. Standard gradient-based descriptors (SIFT/ORB) mistake crater floors for crater rims, failing completely.
*   **The Winning Implementation:** Use **Phase Congruency (e.g., RIFT - Radiation-variation Insensitive Feature Transform)**. Phase congruency detects structural edges based on the phase of Fourier components rather than pixel intensity gradients. This guarantees that a crater is recognized as a crater, regardless of whether the shadow falls to the east or west.

### 2. Enforced Spatial Uniformity (Solving the Explicit ISRO Requirement)
*   **The Problem:** Lunar terrain has highly textured highlands and smooth, featureless maria. Match points naturally cluster in the highly textured areas, causing severe warping distortions in the featureless zones when applying transformations.
*   **The Winning Implementation:** Implement a **Quad-Tree Adaptive Non-Maximal Suppression (ANMS)** algorithm combined with a strict Grid-Based Feature Allocation.
*   **Proof for Jury:** The automated evaluation metrics must natively calculate and output a **Spatial Distribution Index (SDI)**, empirically proving to the judges that the tie-points are evenly distributed across the entire image frame.

### 3. Hyperspectral-to-Panchromatic Bridging (IIRS to OHRC/TMC)
*   **The Problem:** Matching a multi-band hyperspectral image (IIRS) to a high-resolution panchromatic image (OHRC/TMC).
*   **The Winning Implementation:** Implement an automated pre-processing step that calculates the Signal-to-Noise Ratio (SNR) of all IIRS bands, dynamically drops the noisy ones, and applies **Principal Component Analysis (PCA)**. The 1st Principal Component is then extracted and used as a structural pseudo-panchromatic map to perfectly match the OHRC/TMC data.

### 4. Rigorous "Mission-Ready" Evaluation Rig
*   **The Problem:** Visually overlapping two images is not enough for space engineers; they require mathematical proof of accuracy.
*   **The Winning Implementation:** Do not use all matched points for the transformation. Automatically split the matched tie-points into **Control Points** (used to calculate the spatial warp) and **Independent Check Points** (held out entirely). Calculate the Sub-Pixel Root Mean Square Error (RMSE) *only* on the Check Points. 

---

## PART 2: The Ideal Solution Blueprint (What to Build)

This is the comprehensive end-to-end architecture that the prototype must contain to be considered the "gold standard" solution.

### Step 1: Pre-Processing & Modality Harmonization
*   **Radiometric Normalization:** Apply Min-Max stretching and Contrast Limited Adaptive Histogram Equalization (CLAHE) to normalize visual contrast across vastly different sensors.
*   **Scale Normalization (Pyramiding):** Read the Ground Sample Distance (GSD) from the GeoTIFF metadata. Down-sample the ultra-high-resolution image (e.g., OHRC at 0.25m) to roughly match the lower-resolution reference (e.g., IIRS at ~20m) before initiating feature extraction.

### Step 2: Coarse Global Alignment
*   **Algorithm:** **LoFTR (Local Feature TRansformer)**.
*   **Purpose:** Since a 0.25m image cannot be directly patch-matched to a 20m image, use LoFTR on coarse-level feature maps to establish a global structural understanding.
*   **Action:** Estimate an initial Affine Transformation using **MAGSAC++** (a robust RANSAC variant) to apply a rough, initial warp to the source image, bringing it into general alignment with the reference.

### Step 3: Grid-Based Fine Matching (The Uniformity Engine)
*   **Grid Partitioning:** Divide both the coarsely aligned source and the reference images into an $N 	imes M$ grid.
*   **Feature Extraction:** Run **RIFT** (or SuperPoint) independently inside every single grid cell.
*   **Filtering:** Apply the **ANMS Quad-Tree** algorithm within each cell to extract exactly $K$ top keypoints, ensuring they are spatially spread out.

### Step 4: Sub-Pixel Refinement
*   **Patch Extraction:** Extract 16x16 or 32x32 pixel windows around every matched tie-point from Step 3.
*   **Fourier Phase Correlation:** Apply 2D Phase Correlation using Fast Fourier Transform (FFT).
*   **Peak Fitting:** Fit a 2D Gaussian curve to the peak of the cross-power spectrum to determine the exact geometric translation offset down to a 0.05 sub-pixel accuracy.

### Step 5: Outlier Rejection & Non-Rigid Transformation
*   **Final Filtering:** Run **MAGSAC++** one last time on the sub-pixel refined points to discard any statistical outliers.
*   **Transformation Engine:** Apply a **Thin Plate Spline (TPS)** transformation. Unlike basic homography (which assumes the moon is completely flat), TPS allows for non-rigid, local deformations to accurately warp the image over deep craters and high elevation reliefs.

---

## PART 3: Expected Deliverables & Software Package

To win, the final submission must be a complete, deployable software package, not just a Jupyter Notebook.

1.  **The Executable Interface:** 
    *   **CLI:** `python register.py --source ohrc.tif --ref lro.tif --out aligned.tif --metrics report.json`
    *   **GUI:** A visualizer (Streamlit/PyQt) with a side-by-side swipe tool and checkerboard overlay.
2.  **Registered Image Products:** The final transformed GeoTIFFs, preserving all spatial metadata.
3.  **Correspondence Data:** A CSV file containing all final Sub-Pixel Match Points (Source X, Source Y, Ref X, Ref Y).
4.  **Evaluation Metrics Report:** An automated JSON/PDF output detailing:
    *   Sub-Pixel RMSE (on check points).
    *   Inlier Ratio (> 85%).
    *   Spatial Distribution Index (SDI).
5.  **Containerization:** A `Dockerfile` bundling `GDAL`, `PyTorch`, `OpenCV`, and `rasterio` so the ISRO judges can run it immediately without dependency hell.
