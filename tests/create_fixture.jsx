/* Synthetic regression figure. Run with FC_FIXTURE_PATH set to a new local .ai path. */
(function () {
    var old = app.userInteractionLevel;
    var previous = app.documents.length ? app.activeDocument : null;
    var doc = null;
    function color(r,g,b) { var c = new RGBColor(); c.red=r; c.green=g; c.blue=b; return c; }
    try {
        app.userInteractionLevel = UserInteractionLevel.DONTDISPLAYALERTS;
        doc = app.documents.add(DocumentColorSpace.RGB, 432, 288);
        doc.artboards[0].name = "Gradient, dashes & text";
        var background = doc.pathItems.rectangle(280, 8, 416, 272);
        background.fillColor = color(248,248,244); background.stroked=false;
        var title = doc.textFrames.add(); title.contents = "Scientific figure  |  α + β = 0.05";
        title.position = [24, 262]; title.textRange.characterAttributes.size=15;
        var gradient = doc.gradients.add(); gradient.type=GradientType.LINEAR;
        gradient.gradientStops[0].color=color(15,118,110);
        gradient.gradientStops[1].color=color(245,178,70);
        var fill=new GradientColor(); fill.gradient=gradient; fill.angle=0;
        var panel = doc.pathItems.rectangle(226,24,240,110);
        panel.stroked=false; panel.fillColor=fill;
        var line=doc.pathItems.add(); line.setEntirePath([[24,92],[264,92]]);
        line.stroked=true; line.strokeWidth=2.25; line.strokeDashes=[9,4]; line.filled=false;
        line.strokeColor=color(20,40,60);
        var curve=doc.pathItems.add(); curve.setEntirePath([[24,46],[110,70],[186,40],[264,72]]);
        curve.stroked=true; curve.strokeWidth=1.5; curve.strokeDashes=[3,3]; curve.filled=false;
        curve.strokeColor=color(110,55,160);
        var circle=doc.pathItems.ellipse(219,288,90,90);
        circle.fillColor=color(30,120,200); circle.stroked=false; circle.opacity=48;
        var second=doc.pathItems.ellipse(190,317,90,90);
        second.fillColor=color(220,80,80); second.stroked=false; second.opacity=55;
        var note=doc.textFrames.add(); note.contents="Dashed strokes · 600 dpi"; note.position=[24,30];
        note.textRange.characterAttributes.size=11;
        doc.artboards.add([470,288,686,144]); doc.artboards[1].name="Second board";
        var marker=doc.pathItems.rectangle(276,482,192,120); marker.fillColor=color(15,118,110); marker.stroked=false;
        var other=doc.textFrames.add(); other.contents="BOARD TWO"; other.position=[500,225];
        other.textRange.characterAttributes.size=18; other.textRange.characterAttributes.fillColor=color(255,255,255);
        var locked = doc.layers.add(); locked.name="Visible locked text";
        var label=locked.textFrames.add(); label.contents="Locked text 123"; label.position=[286,85];
        label.textRange.characterAttributes.size=12; locked.locked=true;
        var hidden = doc.layers.add(); hidden.name="MUST REMAIN HIDDEN";
        var red = hidden.pathItems.rectangle(288,0,432,288); red.fillColor=color(255,0,0); red.stroked=false;
        hidden.visible=false;
        var options=new IllustratorSaveOptions(); options.pdfCompatible=true; options.compressed=true;
        doc.saveAs(new File(FC_FIXTURE_PATH), options);
        return "created " + FC_FIXTURE_PATH;
    } finally {
        if(doc) doc.close(SaveOptions.DONOTSAVECHANGES);
        if(previous) try {app.activeDocument=previous;} catch(e) {}
        app.userInteractionLevel=old;
    }
})();
