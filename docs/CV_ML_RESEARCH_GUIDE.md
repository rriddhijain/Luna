# 📚 CV/ML Matching: Research & Implementation Guide

This guide compiles the key research papers and implementation strategies for the **CV/ML Matching Seat (Seat ①)**. Studying and implementing these patterns will move our registration system from a standard prototype to a state-of-the-art implementation.

---

## 1. RIFT: Radiation-Variation Insensitive Feature Transform
* **Paper Reference**: *RIFT: Multi-Modal Image Registration Based on Radiation-Variation Insensitive Feature Transform* (Li et al., 2018)
* **Core Problem Solved**: Classical matchers like SIFT/ORB fail when the sun angle changes because they rely directly on pixel brightness. If shadows move, descriptors change completely.
* **Key Concept**: RIFT uses **Phase Congruency (PC)** maps rather than image intensity. PC values measure structural transitions in the frequency domain, which are invariant to illumination changes.
* **What to Implement Next (L3 Descriptor)**:
  * Extract multi-scale Log-Gabor filter responses at different orientations.
  * Construct a **Maximum Index Map (MIM)**: at each pixel, assign the index of the orientation that has the maximum Log-Gabor filter response.
  * Build matching patch descriptors over the MIM map. This provides a robust, hand-crafted, illumination-invariant descriptor.

---

## 2. LoFTR: Detector-Free Local Feature Matching with Transformers
* **Paper Reference**: *LoFTR: Detector-Free Local Feature Matching with Transformers* (Sun et al., 2021)
* **Core Problem Solved**: Low-texture areas on the lunar surface (plains, dust) don't have distinct corners. Classical keypoint detectors (FAST/SIFT) find zero points, halting the pipeline.
* **Key Concept**: LoFTR uses a **detector-free** model. It processes images using a Convolutional Neural Network (CNN) to get features, then feeds them into self-attention and cross-attention transformer layers to match features directly at a coarse grid level.
* **Implementation Focus**:
  * We have integrated Kornia's pre-trained LoFTR.
  * Focus on tuning the `score_threshold` (confidence) to balance match density vs. outlier generation.

---

## 3. LightGlue: Local Feature Matching at Light Speed
* **Paper Reference**: *LightGlue: Local Feature Matching at Light Speed* (Lindenberger et al., 2023)
* **Core Problem Solved**: Transformer-based matchers are slow and memory-intensive, especially on CPU-limited satellite computers.
* **Key Concept**: LightGlue introduces adaptive depth. It predicts match confidence at each attention layer. If the match is highly confident, it stops matching early (layer pruning). It also filters out unmatchable keypoints early.
* **Implementation Focus**:
  * If CPU runtimes for LoFTR become a blocker, implement LightGlue on top of extracted keypoints (like SuperPoint + LightGlue) as a faster, learned alternative.

---

## 4. MAGSAC++: Margins Consensus Robust Estimator
* **Paper Reference**: *MAGSAC++, a fast, reliable and accurate robust estimator* (Barath et al., 2020)
* **Core Problem Solved**: Traditional RANSAC requires a hard inlier threshold (e.g. 3 pixels). If the threshold is off, valid matches are discarded or bad ones are kept.
* **Key Concept**: MAGSAC++ eliminates the hard threshold by marginalizing over a range of threshold values. It is faster, more accurate, and less sensitive to noise parameter tuning.
* **Crossover Implementation**:
  * Used in `samanvay/geometry/verify.py` to filter out matching mistakes and calculate the homography.
