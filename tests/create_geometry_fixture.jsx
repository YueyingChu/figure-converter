/*
 * Reproducible artboard-geometry regression fixture.
 * Set FC_FIXTURE_PATH to a NEW .ai path in an existing writable directory.
 * Run this script through converter.write_js_script / run_illustrator while
 * holding converter.illustrator_lock. It never opens an existing document.
 *
 * Creates four boards and a <path>.expected.json manifest:
 *   1. Nested rectangular clipping, visible locked art, hidden objects/layers,
 *      opacity-zero groups/layers, guides and unpainted paths.
 *   2. Separate artwork and a rectangular clipping path. Overlarge children detect
 *      accidental cross-board unions.
 *   3. A clipping mask with its own visible fill and 4 pt stroke.
 *   4. A 10 pt square to exercise PNG24 fallback when exporting 180 mm / 600 DPI.
 *
 * The manifest stores hand-calculated bounds, not bounds queried from the
 * implementation under test. Hash the generated AI before/after conversion;
 * the original artboard rectangles and file bytes must remain unchanged.
 */
(function () {
    if (typeof FC_FIXTURE_PATH !== "string" || !/\.ai$/i.test(FC_FIXTURE_PATH)) {
        throw new Error("Set FC_FIXTURE_PATH to a new .ai file path.");
    }
    var destination = new File(FC_FIXTURE_PATH);
    var manifestFile = new File(FC_FIXTURE_PATH + ".expected.json");
    if (destination.exists || manifestFile.exists) {
        throw new Error("Refusing to overwrite an existing fixture or manifest.");
    }
    if (!destination.parent.exists) throw new Error("Fixture directory must already exist.");

    var previous = app.documents.length ? app.activeDocument : null;
    var interaction = app.userInteractionLevel;
    var coordinates = app.coordinateSystem;
    var doc = null;
    function color(r, g, b) {
        var value = new RGBColor();
        value.red = r; value.green = g; value.blue = b;
        return value;
    }
    function box(container, name, left, top, right, bottom, fill) {
        var item = container.pathItems.rectangle(top, left, right-left, top-bottom);
        item.name = name;
        item.filled = true; item.stroked = false;
        item.fillColor = fill;
        return item;
    }
    function mask(container, name, left, top, right, bottom) {
        var item = box(container, name, left, top, right, bottom, color(255,255,255));
        item.filled = false;
        item.clipping = true;
        item.zOrder(ZOrderMethod.BRINGTOFRONT);
        container.clipped = true;
        return item;
    }
    function encode(value) {
        if (value === null) return "null";
        if (typeof value === "string") return '"' + value.replace(/\\/g, "\\\\").replace(/"/g, '\\"') + '"';
        if (typeof value === "number" || typeof value === "boolean") return String(value);
        var parts = [], key;
        if (value instanceof Array) {
            for (key=0; key<value.length; key++) parts.push(encode(value[key]));
            return "[" + parts.join(",") + "]";
        }
        for (key in value) if (value.hasOwnProperty(key)) parts.push(encode(key) + ":" + encode(value[key]));
        return "{" + parts.join(",") + "}";
    }
    try {
        app.userInteractionLevel = UserInteractionLevel.DONTDISPLAYALERTS;
        app.coordinateSystem = CoordinateSystem.DOCUMENTCOORDINATESYSTEM;
        doc = app.documents.add(DocumentColorSpace.RGB, 400, 300);
        doc.artboards[0].artboardRect = [0,300,400,0];
        doc.artboards[0].name = "Nested clips and locked art";
        doc.artboards.add([500,300,800,0]);
        doc.artboards[1].name = "Separate board and rectangular clip";
        doc.artboards.add([900,300,1100,100]);
        doc.artboards[2].name = "Painted mask fill and stroke";
        doc.artboards.add([1200,300,1210,290]);
        doc.artboards[3].name = "10 pt square PNG scaling";

        var art = doc.layers[0];
        art.name = "Visible artwork";
        doc.activeLayer = art;
        box(art, "Board one central rectangle", 100,220,260,120, color(30,120,160));

        var lockedLayer = doc.layers.add();
        lockedLayer.name = "Visible locked layer and path must count";
        var locked = box(lockedLayer, "Extends crop to x60", 60,200,90,140, color(240,160,40));
        locked.locked = true;
        // Illustrator checks the active layer even for creation through an
        // explicitly specified parent collection. Keep it editable throughout.
        doc.activeLayer = art;
        lockedLayer.locked = true;

        var outer = art.groupItems.add();
        outer.name = "Outer clip limits top and bottom";
        var inner = outer.groupItems.add();
        inner.name = "Inner clip limits left and right";
        box(inner, "Huge child must not enlarge either artboard", 0,290,800,10, color(50,180,100));
        mask(inner, "Inner 30x180 mask", 290,250,320,70);
        mask(outer, "Outer 60x160 mask", 280,240,340,80);
        // Intersection of the two masks is [290,240,320,80]. Combined with
        // the locked rectangle, board one's final bounds are [60,240,320,80].

        box(art, "Board two central rectangle", 540,240,660,120, color(160,80,200));
        var separateGroup = art.groupItems.add();
        separateGroup.name = "Clipping on a separate artboard";
        box(separateGroup, "Huge clipped child on separate board", 0,290,800,10, color(220,90,90));
        mask(separateGroup, "Separate 60x60 mask", 680,220,740,160);
        // Board two is [540,240,740,120], independent of board one's crop.

        var paintedMaskGroup = art.groupItems.add();
        paintedMaskGroup.name = "A painted mask is also visible artwork";
        box(paintedMaskGroup, "Small child cannot define the painted mask bounds", 980,220,1020,180, color(30,80,180));
        var paintedMask = mask(paintedMaskGroup, "Visible filled and stroked mask must count", 940,260,1060,140);
        paintedMask.filled = true;
        paintedMask.fillColor = color(245,190,60);
        paintedMask.stroked = true;
        paintedMask.strokeWidth = 4;
        paintedMask.strokeDashes = [];
        paintedMask.strokeColor = color(30,30,30);
        // Its stroke extends 2 pt outside the mask's geometric bounds:
        // board three must be [938,262,1062,138], not the child or inner mask.

        box(art, "Tiny square", 1200,300,1210,290, color(20,130,120));

        var hiddenLayer = doc.layers.add();
        hiddenLayer.name = "Hidden layer must not count";
        box(hiddenLayer, "Hidden full-span rectangle", 0,300,1210,0, color(255,0,0));
        doc.activeLayer = art;
        hiddenLayer.visible = false;
        var hiddenObject = box(art, "Hidden item must not count", 0,300,800,0, color(255,0,0));
        hiddenObject.hidden = true;
        var hiddenGroup = art.groupItems.add();
        hiddenGroup.name = "Hidden ancestor must not count";
        box(hiddenGroup, "Hidden via parent", 0,300,800,0, color(255,0,0));
        hiddenGroup.hidden = true;

        var transparentLayer = doc.layers.add();
        transparentLayer.name = "Zero-opacity layer must not count";
        box(transparentLayer, "Invisible via layer opacity", 0,300,1210,0, color(255,0,0));
        doc.activeLayer = art;
        transparentLayer.opacity = 0;
        var transparentGroup = art.groupItems.add();
        transparentGroup.name = "Zero-opacity group must not count";
        box(transparentGroup, "Invisible via group opacity", 0,300,800,0, color(255,0,0));
        transparentGroup.opacity = 0;
        var unpainted = box(art, "No fill and no stroke must not count", 0,300,800,0, color(255,0,0));
        unpainted.filled = false;
        var guide = art.pathItems.add();
        guide.name = "Diagonal guide must not count";
        guide.setEntirePath([[0,300],[800,0]]);
        guide.filled = false; guide.stroked = false; guide.guides = true;

        doc.artboards.setActiveArtboardIndex(0);
        doc.selection = null;
        var saveOptions = new IllustratorSaveOptions();
        saveOptions.pdfCompatible = true;
        saveOptions.compressed = true;
        doc.saveAs(destination, saveOptions);

        var manifest = {
            fixture:"geometry-v1", source:destination.fsName,
            export_settings:{trim_artboard:true, width_mm:180, dpi:600},
            preserve_source_bytes:true,
            bounds_tolerance_pt:0.01, png_dimension_tolerance_px:1,
            artboards:[
                {index:1, original_bounds:[0,300,400,0], crop_bounds:[60,240,320,80],
                 source_width_pt:260, source_height_pt:160, width_mm:180,
                 height_mm:180*160/260, png_width_px:4252, png_height_px:2617, trimmed:true},
                {index:2, original_bounds:[500,300,800,0], crop_bounds:[540,240,740,120],
                 source_width_pt:200, source_height_pt:120, width_mm:180,
                 height_mm:108, png_width_px:4252, png_height_px:2552, trimmed:true},
                {index:3, original_bounds:[900,300,1100,100], crop_bounds:[938,262,1062,138],
                 source_width_pt:124, source_height_pt:124, width_mm:180,
                 height_mm:180, png_width_px:4252, png_height_px:4252, trimmed:true},
                {index:4, original_bounds:[1200,300,1210,290], crop_bounds:[1200,300,1210,290],
                 source_width_pt:10, source_height_pt:10, width_mm:180,
                 height_mm:180, png_width_px:4252, png_height_px:4252, trimmed:false}
            ]
        };
        manifestFile.encoding = "UTF-8";
        if (!manifestFile.open("w")) throw new Error("Cannot write fixture manifest.");
        try { manifestFile.write(encode(manifest)); } finally { manifestFile.close(); }
        return "Created " + destination.fsName + " and " + manifestFile.fsName;
    } catch (error) {
        throw new Error(String(error.message || error) + (error.line ? " (fixture line " + error.line + ")" : ""));
    } finally {
        if (doc) try { doc.close(SaveOptions.DONOTSAVECHANGES); } catch (closeError) {}
        if (previous) try { app.activeDocument = previous; } catch (e) {}
        app.coordinateSystem = coordinates;
        app.userInteractionLevel = interaction;
    }
})();
