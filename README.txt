SMART SOLID WASTE COLLECTION – SOFTWARE DEMONSTRATION

FILES
- app.py: Streamlit application
- requirements.txt: required Python packages

SETUP
1. Install Python 3 from https://www.python.org/downloads/
2. Open a terminal / Command Prompt in this folder.
3. Install packages:
   python -m pip install -r requirements.txt
4. Start the app:
   python -m streamlit run app.py
5. A browser tab should open with the dashboard. If it does not, copy the
   Local URL shown in the terminal (usually http://localhost:8501).

USING THE DEMO
- Use the sliders in the left sidebar to change each bin's fill level.
- Bins at or above the selected threshold are marked COLLECT.
- The app calculates a nearest-neighbour route from the depot and back.
- It compares that route with a fixed-order route.
- Click Reset sample data to restore the report's sample fill levels.

IMPORTANT PROJECT NOTES
- The report's sample fill levels are used: 92, 48, 86, 35, 79, 67, 96, 81.
- The collection threshold defaults to 75%.
- RFID IDs and ultrasonic readings are simulated in software.
- The 2D coordinates are illustrative grid values created for this demo;
  they are not real GPS or road coordinates.
- Distances are Euclidean grid distances, not actual travel distances.
- Nearest-neighbour is a heuristic and does not guarantee the globally
  shortest route.
