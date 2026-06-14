GTPS2MotionTool v2.0: Blender add-on for importing and exporting GT4/TT motion (.mot) files.

Examples of Custom Motion:
<img width="1717" height="966" alt="CustomMotTest" src="https://github.com/user-attachments/assets/1bcfdaa4-fcb1-44db-a38a-b3fb38a9c6cb" />

Features: 
- Imports & exports character motion to GT4/TT
- Imports & exports camera/light motion to GT4/TT
- Supports most character motion
- Animation can have custom interpolation

Flaws:
- Adding custom animation from a different skeleton isn't supported yet due to game engine using custom skeletal structure (Vice versa with GT motion to a new skeleton).
- Multi-character motion isn't supported yet.

Requirements:
- Blender version 3.6.0 To 4.5.0 only (Doesn't work with +5.0.0 Versions of blender.)
- Basic blender knowledge.

How to install:

<img width="800" height="450" alt="HowToInstall1" src="https://github.com/user-attachments/assets/c2cd2894-cb1d-4cd1-b26a-9e336d5ca441" />

How to use: File > Import GT4/TT .mot file > Select the Mot type from the side UI > YourMotionFile.Mot 
- For exporting a Mot file the tool auto-detects which type of Mot you selected and will export it as the selected Mot.

<img width="405" height="720" alt="ImportExportType" src="https://github.com/user-attachments/assets/cf89ecff-c22e-4fbd-8aa9-d19dea530a09" />

- Special thanks for orewa.w's help in figuring out the export motion size <3
