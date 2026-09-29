# Third-Party Notices

## Signora AI Retrieval Engine v1.0

This document identifies third-party software, libraries, frameworks, models, datasets, services, assets, and other materials that may be used by or referenced by the **Signora AI Retrieval Engine v1.0**.

The presence of a third-party component in this repository does **not** mean that the authors of Signora AI claim copyright ownership over that component.

All third-party materials remain the property of their respective copyright holders and are subject to their respective licenses and terms of use.

---

# 1. Python and Python Packages

The project may depend on Python and third-party Python packages installed through `pip`, `requirements.txt`, or another package-management mechanism.

Examples may include:

- NumPy
- Pandas
- FastAPI
- Uvicorn
- Pydantic
- Scikit-learn
- Sentence Transformers
- FAISS
- PyTorch
- OpenCV
- Other packages listed in `requirements.txt`

These packages are **not original Signora AI source code**.

Their copyrights and licenses remain with their respective authors and maintainers.

The definitive dependency list should be obtained from the repository's dependency files.

---

# 2. JavaScript / TypeScript Dependencies

If the project includes a web interface or browser-based motion player, it may use third-party software such as:

- React
- Vite
- Three.js
- Node.js packages
- npm packages
- UI libraries

These dependencies are governed by their respective licenses.

Directories such as:

```text
node_modules/
dist/
build/
```

should not be treated as original copyrighted source code of Signora AI.

---

# 3. Speech Recognition

If Signora AI uses **OpenAI Whisper** or another speech-recognition technology, the underlying model, model weights, source code, and associated materials are third-party works.

Signora AI claims authorship only over its own integration, processing logic, orchestration, configuration, or application-specific implementation.

---

# 4. Machine-Learning Models

Any pretrained models used by the system remain subject to the original model creators' licenses.

Examples may include:

- Embedding models
- Sentence-transformer models
- Speech-recognition models
- Pose-estimation models
- Computer-vision models
- NLP models

Model weights should not be represented as original Signora AI software unless they were independently created and trained by the Signora AI authors.

---

# 5. FAISS / Vector Search

If FAISS or another vector-search implementation is used, the underlying library remains third-party software.

The Signora AI authors may claim copyright only over original code implementing application-specific:

- Index construction
- Metadata handling
- Retrieval orchestration
- Ranking
- Filtering
- Hierarchical fallback
- Motion selection
- Search integration

---

# 6. Blender

Blender is third-party software developed by the Blender Foundation and contributors.

Any use of Blender for:

- Motion inspection
- BVH processing
- Retargeting
- GLB export
- Animation validation

does not imply ownership of Blender itself.

Original scripts independently written by the Signora AI team for use with Blender may remain original project software, subject to any applicable Blender licensing requirements.

---

# 7. Rokoko

If Rokoko software, plugins, retargeting functionality, skeleton mappings, or related services are used, those components remain the property of Rokoko and their respective rights holders.

Signora AI does not claim ownership over Rokoko software or proprietary technology.

Original scripts written around such tools should be distinguished from the third-party tools themselves.

---

# 8. Three-Dimensional Models and Avatars

Any third-party:

- GLB
- GLTF
- FBX
- OBJ
- Avatar
- Character model
- Rig
- Texture
- Material
- Animation asset

must remain subject to its original license.

Only independently created project assets may be claimed as original copyrighted material.

Before copyright filing, document the origin of every major 3D asset used by the system.

---

# 9. BVH and Motion-Capture Data

Motion files obtained from external datasets, public repositories, motion-capture libraries, research datasets, collaborators, or third-party tools remain subject to their respective ownership and licensing conditions.

This includes:

```text
*.bvh
*.fbx
*.glb
*.gltf
*.npy
```

unless the files were independently created by the Signora AI team.

The software code used to process, normalize, retrieve, convert, index, rank, stitch, or display those motions may be separately copyrightable as original software.

---

# 10. Indian Sign Language Data

Indian Sign Language vocabulary, signing videos, dictionaries, gloss resources, educational resources, or datasets obtained from external sources are not automatically owned by the Signora AI authors.

For every external ISL dataset or resource, record:

- Resource name
- Creator/organization
- Source
- License
- Permission status
- Intended use

Do not claim external signing videos or annotations as original project assets unless they were independently created with appropriate participant consent and ownership documentation.

---

# 11. Fonts, Icons, Images, and UI Assets

Third-party:

- Fonts
- Icons
- Logos
- Images
- Illustrations
- UI components

remain subject to their original licenses.

Examples may include assets obtained from:

- Google Fonts
- Font Awesome
- Lucide
- Material Icons
- Stock-image sources
- Open-source UI libraries

Only independently created visual assets should be identified as original Signora AI material.

---

# 12. External APIs and Services

The system may integrate with external APIs or hosted services.

Use of an API does not confer ownership of the service or underlying software.

Only original Signora AI integration code may be treated as project source code.

API keys, passwords, credentials, and secrets must **never** be included in the repository or copyright-submission package.

---

# 13. Open-Source Code

Any source code copied, adapted, derived, or substantially based on another open-source project must retain the notices required by the applicable license.

Before copyright filing, review the repository for:

- Copied GitHub code
- Stack Overflow snippets
- Tutorial code
- Example projects
- Copied Blender scripts
- Copied retrieval implementations
- Copied API templates

Material that is not independently authored should not be represented as exclusively original Signora AI software.

---

# 14. Dependency Directories

Generated dependency directories should not be included as original project source code.

Examples:

```text
node_modules/
venv/
.venv/
env/
__pycache__/
.pytest_cache/
.mypy_cache/
dist/
build/
.next/
```

These should generally be excluded from version control and copyright-submission source material.

---

# 15. Original Signora AI Components

Subject to authorship verification, the project's original components may include implementation of:

- Railway-announcement normalization
- Announcement parsing
- Critical entity extraction orchestration
- Safety-validation workflow
- ISL-oriented processing logic
- Hierarchical sentence-level retrieval
- Phrase-level retrieval
- Word-level retrieval
- Number retrieval
- Fingerspelling fallback
- Retrieval ranking and scoring
- Motion metadata lookup
- Retrieval fallback orchestration
- Transition-aware motion selection
- Motion-sequence construction
- Application-specific motion stitching logic
- Railway-specific retrieval rules
- Backend/API integration
- Testing and evaluation code
- Research benchmarking code

Only independently authored implementations should be claimed.

---

# 16. Third-Party Component Register

Before formal copyright filing, complete this table.

| Component | Type | Source | License | Included in Repository? | Original Project Work? |
|---|---|---|---|---|---|
| Python | Runtime | python.org | PSF License | No/Required externally | No |
| [Package] | Python library | [Source] | [License] | Dependency only | No |
| [Model] | ML model | [Source] | [License] | Yes/No | No |
| [Dataset] | ISL dataset | [Source] | [License] | Yes/No | No |
| [Avatar] | 3D asset | [Source] | [License] | Yes/No | No |
| [Motion dataset] | BVH/GLB | [Source] | [License] | Yes/No | No |
| Signora retrieval engine | Source code | Project authors | Original work | Yes | Yes |

Add one row for every significant third-party component.

---

# 17. No Transfer of Third-Party Rights

Nothing in this repository, README, copyright notice, or copyright-registration application should be interpreted as claiming ownership of third-party intellectual property.

Copyright claims relating to Signora AI should be limited to original material independently authored by the project contributors.

---

# 18. Contact / Project Information

**Project:** Signora AI Retrieval Engine v1.0  
**Repository:** `Signora_AI_Retrieval_Engine-v1.0`  
**Primary Maintainer:** Shravan Sapate  
**Version:** 1.0  
**Copyright Filing Version:** `copyright-v1.0`

For copyright-registration purposes, this document should be reviewed and updated before freezing the final release.