param()

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing

$projectRoot = Split-Path -Parent $PSScriptRoot
$mapsRoot = Join-Path $projectRoot 'docs\worldbuilding\maps'
$politicalDir = Join-Path $projectRoot 'docs\worldbuilding\maps\political'

function New-DrawColor {
    param([string]$Hex, [int]$Alpha = 255)

    $clean = $Hex.TrimStart('#')
    return [System.Drawing.Color]::FromArgb(
        $Alpha,
        [Convert]::ToInt32($clean.Substring(0, 2), 16),
        [Convert]::ToInt32($clean.Substring(2, 2), 16),
        [Convert]::ToInt32($clean.Substring(4, 2), 16)
    )
}

function New-PointArray {
    param([int[][]]$Coordinates)

    $points = [System.Drawing.Point[]]::new($Coordinates.Count)
    for ($index = 0; $index -lt $Coordinates.Count; $index++) {
        $points[$index] = [System.Drawing.Point]::new($Coordinates[$index][0], $Coordinates[$index][1])
    }
    return ,$points
}

function Draw-Region {
    param(
        [System.Drawing.Graphics]$Graphics,
        [System.Drawing.Bitmap]$Terrain,
        [int[][]]$Coordinates,
        [string]$Color
    )

    $points = New-PointArray $Coordinates
    $overlay = [System.Drawing.Bitmap]::new($Terrain.Width, $Terrain.Height)
    $overlayGraphics = [System.Drawing.Graphics]::FromImage($overlay)
    $fill = [System.Drawing.SolidBrush]::new((New-DrawColor $Color 82))
    $overlayGraphics.FillPolygon($fill, $points)
    $left = [Math]::Max(0, (($Coordinates | ForEach-Object { $_[0] } | Measure-Object -Minimum).Minimum))
    $top = [Math]::Max(0, (($Coordinates | ForEach-Object { $_[1] } | Measure-Object -Minimum).Minimum))
    $right = [Math]::Min($Terrain.Width - 1, (($Coordinates | ForEach-Object { $_[0] } | Measure-Object -Maximum).Maximum))
    $bottom = [Math]::Min($Terrain.Height - 1, (($Coordinates | ForEach-Object { $_[1] } | Measure-Object -Maximum).Maximum))
    for ($y = $top; $y -le $bottom; $y++) {
        for ($x = $left; $x -le $right; $x++) {
            $terrainPixel = $Terrain.GetPixel($x, $y)
            $isWater = ($terrainPixel.B -gt ($terrainPixel.R * 1.18)) -and ($terrainPixel.B -gt ($terrainPixel.G * 1.05))
            if ($isWater) {
                $overlay.SetPixel($x, $y, [System.Drawing.Color]::Transparent)
            }
        }
    }
    $Graphics.DrawImageUnscaled($overlay, 0, 0)
    $fill.Dispose()
    $overlayGraphics.Dispose()
    $overlay.Dispose()
}

function Draw-Boundary {
    param(
        [System.Drawing.Graphics]$Graphics,
        [int[][]]$Coordinates,
        [string]$Color
    )

    $points = New-PointArray $Coordinates
    $outerPen = [System.Drawing.Pen]::new((New-DrawColor '#10131B' 235), 10)
    $innerPen = [System.Drawing.Pen]::new((New-DrawColor $Color 255), 4)
    $outerPen.LineJoin = [System.Drawing.Drawing2D.LineJoin]::Round
    $innerPen.LineJoin = [System.Drawing.Drawing2D.LineJoin]::Round
    $Graphics.DrawCurve($outerPen, $points, 0.42)
    $Graphics.DrawCurve($innerPen, $points, 0.42)
    $outerPen.Dispose()
    $innerPen.Dispose()
}

function Draw-AdminBoundary {
    param(
        [System.Drawing.Graphics]$Graphics,
        [int[][]]$Coordinates,
        [string]$Color
    )

    $points = New-PointArray $Coordinates
    $pen = [System.Drawing.Pen]::new((New-DrawColor $Color 210), 2)
    $pen.DashStyle = [System.Drawing.Drawing2D.DashStyle]::Dash
    $Graphics.DrawCurve($pen, $points, 0.35)
    $pen.Dispose()
}

function Draw-Label {
    param(
        [System.Drawing.Graphics]$Graphics,
        [int]$X,
        [int]$Y,
        [string]$Title,
        [string]$Species,
        [string]$Admin,
        [string]$Color
    )

    $titleFont = [System.Drawing.Font]::new('Microsoft YaHei', 24, [System.Drawing.FontStyle]::Bold)
    $speciesFont = [System.Drawing.Font]::new('Microsoft YaHei', 15, [System.Drawing.FontStyle]::Regular)
    $adminFont = [System.Drawing.Font]::new('Microsoft YaHei', 12, [System.Drawing.FontStyle]::Regular)
    $format = [System.Drawing.StringFormat]::new()
    $format.Alignment = [System.Drawing.StringAlignment]::Center
    $format.LineAlignment = [System.Drawing.StringAlignment]::Center
    $titleSize = $Graphics.MeasureString($Title, $titleFont)
    $speciesSize = $Graphics.MeasureString($Species, $speciesFont)
    $adminSize = $Graphics.MeasureString($Admin, $adminFont)
    $width = [Math]::Max([Math]::Max($titleSize.Width, $speciesSize.Width), $adminSize.Width) + 28
    $height = $titleSize.Height + $speciesSize.Height + $adminSize.Height + 20
    $rect = [System.Drawing.RectangleF]::new($X - $width / 2, $Y - $height / 2, $width, $height)
    $boxBrush = [System.Drawing.SolidBrush]::new((New-DrawColor '#10131B' 196))
    $borderPen = [System.Drawing.Pen]::new((New-DrawColor $Color 255), 2)
    $whiteBrush = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::White)
    $speciesBrush = [System.Drawing.SolidBrush]::new((New-DrawColor '#F5E6C8' 255))
    $adminBrush = [System.Drawing.SolidBrush]::new((New-DrawColor '#B9D2DD' 255))
    $Graphics.FillRectangle($boxBrush, $rect)
    $Graphics.DrawRectangle($borderPen, $rect.X, $rect.Y, $rect.Width, $rect.Height)
    $Graphics.DrawString($Title, $titleFont, $whiteBrush, [System.Drawing.RectangleF]::new($rect.X, $rect.Y + 2, $rect.Width, $titleSize.Height), $format)
    $Graphics.DrawString($Species, $speciesFont, $speciesBrush, [System.Drawing.RectangleF]::new($rect.X, $rect.Y + $titleSize.Height, $rect.Width, $speciesSize.Height), $format)
    $Graphics.DrawString($Admin, $adminFont, $adminBrush, [System.Drawing.RectangleF]::new($rect.X, $rect.Y + $titleSize.Height + $speciesSize.Height, $rect.Width, $adminSize.Height), $format)
    $titleFont.Dispose()
    $speciesFont.Dispose()
    $adminFont.Dispose()
    $format.Dispose()
    $boxBrush.Dispose()
    $borderPen.Dispose()
    $whiteBrush.Dispose()
    $speciesBrush.Dispose()
    $adminBrush.Dispose()
}

function Draw-City {
    param(
        [System.Drawing.Graphics]$Graphics,
        [int]$X,
        [int]$Y,
        [string]$Name
    )

    $outer = [System.Drawing.SolidBrush]::new((New-DrawColor '#10131B' 255))
    $inner = [System.Drawing.SolidBrush]::new((New-DrawColor '#FFF0A6' 255))
    $Graphics.FillEllipse($outer, $X - 8, $Y - 8, 16, 16)
    $Graphics.FillEllipse($inner, $X - 4, $Y - 4, 8, 8)
    $font = [System.Drawing.Font]::new('Microsoft YaHei', 16, [System.Drawing.FontStyle]::Bold)
    $shadow = [System.Drawing.SolidBrush]::new((New-DrawColor '#10131B' 240))
    $text = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::White)
    $Graphics.DrawString($Name, $font, $shadow, $X + 11, $Y - 13)
    $Graphics.DrawString($Name, $font, $text, $X + 9, $Y - 15)
    $outer.Dispose()
    $inner.Dispose()
    $font.Dispose()
    $shadow.Dispose()
    $text.Dispose()
}

function Draw-Legend {
    param(
        [System.Drawing.Graphics]$Graphics,
        [hashtable[]]$Items,
        [string]$Title
    )

    $titleFont = [System.Drawing.Font]::new('Microsoft YaHei', 14, [System.Drawing.FontStyle]::Bold)
    $font = [System.Drawing.Font]::new('Microsoft YaHei', 11, [System.Drawing.FontStyle]::Regular)
    $black = [System.Drawing.SolidBrush]::new((New-DrawColor '#231A12' 255))
    $panel = [System.Drawing.SolidBrush]::new((New-DrawColor '#F3DFC0' 220))
    $panelBorder = [System.Drawing.Pen]::new((New-DrawColor '#5C3A1E' 255), 2)
    $Graphics.FillRectangle($panel, 28, 815, 280, 180)
    $Graphics.DrawRectangle($panelBorder, 28, 815, 280, 180)
    $Graphics.DrawString($Title, $titleFont, $black, 55, 842)
    $y = 866
    foreach ($item in $Items) {
        $swatch = [System.Drawing.SolidBrush]::new((New-DrawColor $item.Color 210))
        $Graphics.FillRectangle($swatch, 58, $y + 3, 12, 12)
        $Graphics.DrawString($item.Name, $font, $black, 78, $y)
        $swatch.Dispose()
        $y += 20
    }
    $titleFont.Dispose()
    $font.Dispose()
    $black.Dispose()
    $panel.Dispose()
    $panelBorder.Dispose()
}

function Render-PoliticalMap {
    param(
        [string]$BasePath,
        [string]$OutputName,
        [hashtable[]]$Regions,
        [hashtable[]]$Boundaries,
        [hashtable[]]$AdminBoundaries,
        [hashtable[]]$Cities,
        [hashtable[]]$Legend,
        [string]$Title
    )

    $outputPath = Join-Path $politicalDir $OutputName
    $source = [System.Drawing.Image]::FromFile($basePath)
    $canvas = [System.Drawing.Bitmap]::new($source.Width, $source.Height)
    $graphics = [System.Drawing.Graphics]::FromImage($canvas)
    $graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $graphics.DrawImage($source, 0, 0, $source.Width, $source.Height)

    foreach ($region in $Regions) {
        Draw-Region $graphics $source $region.Points $region.Color
    }
    foreach ($boundary in $Boundaries) {
        Draw-Boundary $graphics $boundary.Points $boundary.Color
    }
    foreach ($adminBoundary in $AdminBoundaries) {
        Draw-AdminBoundary $graphics $adminBoundary.Points $adminBoundary.Color
    }
    foreach ($region in $Regions) {
        Draw-Label $graphics $region.LabelX $region.LabelY $region.Name $region.Species $region.Admin $region.Color
    }
    foreach ($city in $Cities) {
        Draw-City $graphics $city.X $city.Y $city.Name
    }
    Draw-Legend $graphics $Legend $Title

    $canvas.Save($outputPath, [System.Drawing.Imaging.ImageFormat]::Png)
    $graphics.Dispose()
    $canvas.Dispose()
    $source.Dispose()
}

$norhlandRegions = @(
    @{ Name = '伊莱湖林契约群'; Species = '主导：精灵'; Admin = '行政：镜湖北区 / 灰幕林区'; Color = '#36A6A0'; LabelX = 905; LabelY = 255; Points = @(@(570, 55), @(1160, 60), @(1350, 240), @(1240, 360), @(1100, 400), @(900, 390), @(700, 380), @(590, 340), @(560, 220)) },
    @{ Name = '赫塔尔工坊盟约'; Species = '主导：矮人'; Admin = '行政：西脊炉区 / 海湾工区'; Color = '#9B59B6'; LabelX = 365; LabelY = 520; Points = @(@(65, 170), @(560, 220), @(590, 340), @(600, 350), @(590, 450), @(610, 560), @(650, 680), @(600, 760), @(630, 850), @(370, 965), @(110, 760)) },
    @{ Name = '阿德伦议约王国'; Species = '主导：人类'; Admin = '行政：上锻河区 / 三汇直辖 / 下阿德区'; Color = '#F2C14E'; LabelX = 850; LabelY = 525; Points = @(@(600, 350), @(700, 380), @(800, 360), @(900, 390), @(1010, 370), @(1100, 400), @(1140, 480), @(1120, 580), @(1100, 690), @(1010, 700), @(920, 720), @(830, 700), @(740, 710), @(650, 680), @(610, 560), @(590, 450)) },
    @{ Name = '诺赫兰东岸航盟'; Species = '主导：人类'; Admin = '行政：河口五港 / 东岬岛区'; Color = '#3F7EDB'; LabelX = 1230; LabelY = 550; Points = @(@(1100, 400), @(1240, 360), @(1450, 250), @(1510, 590), @(1390, 840), @(1150, 860), @(1170, 770), @(1100, 690), @(1120, 580), @(1140, 480)) },
    @{ Name = '赤原誓团联盟'; Species = '主导：兽人'; Admin = '行政：赤泉牧道 / 南缘堡区'; Color = '#D9574E'; LabelX = 835; LabelY = 820; Points = @(@(650, 680), @(740, 710), @(830, 700), @(920, 720), @(1010, 700), @(1100, 690), @(1170, 770), @(1150, 860), @(1090, 970), @(740, 970), @(630, 850), @(600, 760)) }
)

$norhlandBoundaries = @(
    @{ Color = '#36A6A0'; Points = @(@(560, 220), @(570, 260), @(590, 300), @(600, 350), @(700, 380), @(800, 360), @(900, 390), @(1010, 370), @(1100, 400), @(1180, 380), @(1240, 360)) },
    @{ Color = '#9B59B6'; Points = @(@(600, 350), @(590, 450), @(610, 560), @(650, 680), @(600, 760), @(630, 850)) },
    @{ Color = '#3F7EDB'; Points = @(@(1100, 400), @(1140, 480), @(1120, 580), @(1100, 690), @(1170, 770), @(1150, 860)) },
    @{ Color = '#D9574E'; Points = @(@(650, 680), @(740, 710), @(830, 700), @(920, 720), @(1010, 700), @(1100, 690)) }
)

$norhlandAdminBoundaries = @(
    @{ Color = '#B9FFFF'; Points = @(@(900, 80), @(880, 170), @(890, 260), @(870, 350)) },
    @{ Color = '#E7B7FF'; Points = @(@(330, 250), @(400, 420), @(470, 580), @(520, 750)) },
    @{ Color = '#FFF0A6'; Points = @(@(770, 390), @(800, 520), @(780, 650)) },
    @{ Color = '#FFF0A6'; Points = @(@(970, 390), @(950, 540), @(970, 690)) },
    @{ Color = '#B9D9FF'; Points = @(@(1260, 410), @(1300, 550), @(1280, 730)) },
    @{ Color = '#FFB5AE'; Points = @(@(880, 750), @(850, 840), @(870, 930)) }
)

$norhlandCities = @(
    @{ X = 855; Y = 660; Name = '澜誓城' },
    @{ X = 480; Y = 640; Name = '锻谷城' },
    @{ X = 890; Y = 345; Name = '望镜湖庭' },
    @{ X = 1275; Y = 650; Name = '东澜港' },
    @{ X = 865; Y = 920; Name = '赭泉关' }
)

$seraphilRegions = @(
    @{ Name = '绿冠河林共同体'; Species = '主导：精灵'; Admin = '行政：北冠河段 / 东林水网'; Color = '#35A05A'; LabelX = 770; LabelY = 205; Points = @(@(100, 80), @(1380, 90), @(1470, 290), @(1300, 390), @(1120, 360), @(900, 340), @(700, 370), @(450, 360), @(350, 450), @(180, 330)) },
    @{ Name = '卡尔萨高原议盟'; Species = '主导：人类'; Admin = '行政：阶泉梯田 / 西山谷水库'; Color = '#E4A72A'; LabelX = 785; LabelY = 480; Points = @(@(450, 360), @(700, 370), @(900, 340), @(1120, 360), @(1180, 470), @(1120, 580), @(920, 680), @(720, 660), @(520, 650), @(350, 510)) },
    @{ Name = '伊什拉绿洲路盟'; Species = '主导：地精'; Admin = '行政：九井驿路 / 东旱地'; Color = '#C66C3D'; LabelX = 1030; LabelY = 605; Points = @(@(1120, 360), @(1300, 390), @(1430, 610), @(1390, 760), @(1120, 840), @(950, 760), @(920, 680), @(1120, 580), @(1180, 470)) },
    @{ Name = '南潮城邦同盟'; Species = '主导：人类'; Admin = '行政：潮门四港 / 南湾果区'; Color = '#3D82B8'; LabelX = 665; LabelY = 820; Points = @(@(350, 510), @(520, 650), @(720, 660), @(920, 680), @(950, 760), @(1120, 840), @(1080, 970), @(420, 970), @(160, 800)) }
)

$seraphilBoundaries = @(
    @{ Color = '#35A05A'; Points = @(@(180, 330), @(350, 450), @(450, 360), @(700, 370), @(900, 340), @(1120, 360), @(1300, 390)) },
    @{ Color = '#E4A72A'; Points = @(@(350, 510), @(520, 650), @(720, 660), @(920, 680)) },
    @{ Color = '#C66C3D'; Points = @(@(1120, 360), @(1180, 470), @(1120, 580), @(920, 680), @(950, 760), @(1120, 840)) }
)

$seraphilAdminBoundaries = @(
    @{ Color = '#B5FFBF'; Points = @(@(740, 100), @(720, 220), @(760, 330)) },
    @{ Color = '#FFF0A6'; Points = @(@(720, 390), @(760, 520), @(720, 640)) },
    @{ Color = '#FFF0A6'; Points = @(@(930, 390), @(980, 520), @(920, 640)) },
    @{ Color = '#FFCCB5'; Points = @(@(1180, 440), @(1160, 600), @(1200, 760)) },
    @{ Color = '#B9D9FF'; Points = @(@(640, 710), @(660, 820), @(620, 930)) }
)

$seraphilCities = @(
    @{ X = 725; Y = 320; Name = '冠枝河庭' },
    @{ X = 745; Y = 570; Name = '阶泉城' },
    @{ X = 1100; Y = 700; Name = '九泉驿城' },
    @{ X = 610; Y = 905; Name = '南潮门港' }
)

$vestaRegions = @(
    @{ Name = '火弧祭盟'; Species = '主导：矮人'; Admin = '行政：北火岛链 / 烬湾避难区'; Color = '#D85D3B'; LabelX = 235; LabelY = 430; Points = @(@(40, 100), @(430, 110), @(470, 320), @(430, 520), @(390, 720), @(180, 820), @(55, 690)) },
    @{ Name = '中裂海航盟'; Species = '主导：人类'; Admin = '行政：灯庭内海 / 中峡航道'; Color = '#37A2B8'; LabelX = 760; LabelY = 430; Points = @(@(400, 180), @(1050, 100), @(1230, 300), @(1180, 500), @(1130, 650), @(900, 700), @(650, 690), @(420, 650), @(470, 320)) },
    @{ Name = '东门群岛港邦'; Species = '主导：人类'; Admin = '行政：东门主港 / 外环群岛'; Color = '#9B6CC6'; LabelX = 1290; LabelY = 355; Points = @(@(1220, 130), @(1490, 160), @(1510, 590), @(1300, 650), @(1180, 500), @(1230, 300)) },
    @{ Name = '黑潮岛盟'; Species = '主导：兽人'; Admin = '行政：避潮岛链 / 南深潮'; Color = '#415E9A'; LabelX = 850; LabelY = 805; Points = @(@(420, 650), @(650, 690), @(900, 700), @(1130, 650), @(1300, 650), @(1390, 920), @(930, 1000), @(560, 910), @(380, 800)) }
)

$vestaBoundaries = @(
    @{ Color = '#D85D3B'; Points = @(@(400, 180), @(430, 250), @(470, 320), @(420, 450), @(430, 520), @(390, 720)) },
    @{ Color = '#9B6CC6'; Points = @(@(1230, 300), @(1180, 400), @(1180, 500), @(1300, 650)) },
    @{ Color = '#415E9A'; Points = @(@(420, 650), @(650, 690), @(900, 700), @(1130, 650), @(1300, 650)) }
)

$vestaAdminBoundaries = @(
    @{ Color = '#FFCCB5'; Points = @(@(220, 220), @(260, 400), @(220, 650)) },
    @{ Color = '#B9FFFF'; Points = @(@(720, 250), @(760, 430), @(720, 610)) },
    @{ Color = '#D9C4FF'; Points = @(@(1340, 260), @(1300, 430), @(1350, 570)) },
    @{ Color = '#B9C8FF'; Points = @(@(760, 730), @(850, 830), @(930, 930)) }
)

$vestaCities = @(
    @{ X = 230; Y = 520; Name = '烬湾' },
    @{ X = 770; Y = 525; Name = '灯庭' },
    @{ X = 1310; Y = 450; Name = '东门城' },
    @{ X = 840; Y = 880; Name = '避潮岛' }
)

Render-PoliticalMap (Join-Path $mapsRoot 'northern-main-continent.png') 'norhland-political-map.png' $norhlandRegions $norhlandBoundaries $norhlandAdminBoundaries $norhlandCities $norhlandRegions '诺赫兰 · H 24816'
Render-PoliticalMap (Join-Path $mapsRoot 'southern-continent.png') 'seraphil-political-map.png' $seraphilRegions $seraphilBoundaries $seraphilAdminBoundaries $seraphilCities $seraphilRegions '塞拉菲尔 · H 24816'
Render-PoliticalMap (Join-Path $mapsRoot 'eastern-fractured-sea.png') 'vesta-political-map.png' $vestaRegions $vestaBoundaries $vestaAdminBoundaries $vestaCities $vestaRegions '维斯塔裂海 · H 24816'
