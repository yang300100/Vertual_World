[CmdletBinding()]
param(
    [string]$Source,
    [string]$OutputDirectory
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing

if (-not $Source) {
    $Source = Join-Path $PSScriptRoot '..\docs\worldbuilding\maps\planet-master.png'
}
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $PSScriptRoot '..\docs\worldbuilding\maps\tiles\z1'
}

$sourcePath = [System.IO.Path]::GetFullPath($Source)
$outputPath = [System.IO.Path]::GetFullPath($OutputDirectory)
if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
    throw "World map source not found: $sourcePath"
}
[System.IO.Directory]::CreateDirectory($outputPath) | Out-Null

$sourceImage = [System.Drawing.Bitmap]::FromFile($sourcePath)
try {
    if (($sourceImage.Width % 4) -ne 0 -or ($sourceImage.Height % 2) -ne 0) {
        throw 'World map width must divide by 4 and height must divide by 2.'
    }
    $cropWidth = [int]($sourceImage.Width / 4)
    $cropHeight = [int]($sourceImage.Height / 2)
    for ($row = 0; $row -lt 2; $row++) {
        for ($column = 0; $column -lt 4; $column++) {
            $tile = [System.Drawing.Bitmap]::new(768, 1024)
            try {
                $graphics = [System.Drawing.Graphics]::FromImage($tile)
                try {
                    $graphics.CompositingMode = [System.Drawing.Drawing2D.CompositingMode]::SourceCopy
                    $graphics.CompositingQuality = [System.Drawing.Drawing2D.CompositingQuality]::HighQuality
                    $graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
                    $graphics.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
                    $sourceRectangle = [System.Drawing.Rectangle]::new(
                        $column * $cropWidth,
                        $row * $cropHeight,
                        $cropWidth,
                        $cropHeight
                    )
                    $targetRectangle = [System.Drawing.Rectangle]::new(0, 0, 768, 1024)
                    $graphics.DrawImage(
                        $sourceImage,
                        $targetRectangle,
                        $sourceRectangle,
                        [System.Drawing.GraphicsUnit]::Pixel
                    )
                }
                finally {
                    $graphics.Dispose()
                }
                $target = Join-Path $outputPath "r$row-c$column.png"
                $temporary = "$target.tmp.png"
                $tile.Save($temporary, [System.Drawing.Imaging.ImageFormat]::Png)
                Move-Item -LiteralPath $temporary -Destination $target -Force
            }
            finally {
                $tile.Dispose()
            }
        }
    }
}
finally {
    $sourceImage.Dispose()
}

Write-Output "Generated 8 continuous level-1 map tiles: $outputPath"
