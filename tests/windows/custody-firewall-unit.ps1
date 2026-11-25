# Pure policy tests: all Windows NetSecurity operations are mocked in memory.
# Runs with Windows PowerShell 5.1 or pwsh; does not change the host firewall.
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '../../src/potbot/syn_release/custody_firewall.ps1')
$script:ValidateEndpoint = (Get-Command Assert-LiveEndpoint).ScriptBlock

function Assert-Equal($Actual, $Expected, [string]$Message) {
    if (($Actual | ConvertTo-Json -Compress -Depth 8) -cne
        ($Expected | ConvertTo-Json -Compress -Depth 8)) {
        throw ($Message + '; actual=' + ($Actual | ConvertTo-Json -Compress -Depth 8) +
            '; expected=' + ($Expected | ConvertTo-Json -Compress -Depth 8))
    }
}
function Assert-Throws([scriptblock]$Operation, [string]$Message) {
    try { & $Operation } catch {
        if ($_.Exception.Message -like ('*' + $Message + '*')) { return }
        throw
    }
    throw ('Expected failure: ' + $Message)
}
function New-TestRule([string]$Name, [string]$Program = 'C:\uv\python.exe') {
    [pscustomobject]@{
        Name=$Name; DisplayName=$Name; Enabled='True'; Direction='Inbound'; Action='Block'
        Profile=2; PolicyStoreSourceType='Local'; Description=''; Group=''
        Application=[pscustomobject]@{ Program=$Program; Package='Any'; Name=$Name }
        ServiceFilter=[pscustomobject]@{ Service='Any' }
        PortFilter=[pscustomobject]@{ Protocol='TCP'; LocalPort='Any'; RemotePort='Any' }
        AddressFilter=[pscustomobject]@{ LocalAddress='Any'; RemoteAddress='Any' }
        InterfaceFilter=[pscustomobject]@{ InterfaceAlias='Any' }
        TypeFilter=[pscustomobject]@{ InterfaceType='Any' }
        SecurityFilter=[pscustomobject]@{
            Authentication='NotRequired'; LocalUser='Any'; RemoteUser='Any'; RemoteMachine='Any'
        }
    }
}
$script:LiveProgram = 'C:\uv\python.exe'
function Assert-LiveEndpoint($Request) { return $script:LiveProgram }
function Get-NetIPAddress { [pscustomobject]@{ InterfaceIndex=1 } }
function Get-NetConnectionProfile { [pscustomobject]@{ NetworkCategory='Private' } }
function Get-NetFirewallProfile { $script:Profiles }
function Get-NetFirewallRule {
    [CmdletBinding()]
    param($PolicyStore, $Name, [switch]$TracePolicyStore,
        [Parameter(ValueFromPipeline)]$Application)
    process {
        if ($Name) {
            $script:Rules | Where-Object { $_.Name -like $Name }
        } elseif ($Application) {
            $script:Rules | Where-Object { $_.Name -eq $Application.Name }
        } else { $script:Rules }
    }
}
function Get-NetFirewallApplicationFilter {
    param($PolicyStore, [Parameter(ValueFromPipeline)]$Rule)
    process { if ($Rule) { $Rule.Application } else { $script:Rules.Application } }
}
function Get-NetFirewallServiceFilter {
    param([Parameter(ValueFromPipeline)]$Rule) process { $Rule.ServiceFilter }
}
function Get-NetFirewallPortFilter {
    param([Parameter(ValueFromPipeline)]$Rule) process { $Rule.PortFilter }
}
function Get-NetFirewallAddressFilter {
    param([Parameter(ValueFromPipeline)]$Rule) process { $Rule.AddressFilter }
}
function Get-NetFirewallInterfaceFilter {
    param([Parameter(ValueFromPipeline)]$Rule) process { $Rule.InterfaceFilter }
}
function Get-NetFirewallInterfaceTypeFilter {
    param([Parameter(ValueFromPipeline)]$Rule) process { $Rule.TypeFilter }
}
function Get-NetFirewallSecurityFilter {
    param([Parameter(ValueFromPipeline)]$Rule) process { $Rule.SecurityFilter }
}
function New-NetFirewallRule {
    param($Name, $Program, $LocalAddress, $RemoteAddress, $Description, $Group, $DisplayName)
    $new = New-TestRule $Name $Program
    $new.Profile = 0; $new.Action = 'Allow'; $new.Description = $Description; $new.Group = $Group
    $new.DisplayName = $DisplayName
    $new.AddressFilter.LocalAddress = $LocalAddress
    $new.AddressFilter.RemoteAddress = $RemoteAddress
    $script:Rules += $new
}
function Set-NetFirewallRule {
    param($Name, $Program, $LocalAddress, $RemoteAddress, $Enabled, $PolicyStore, $ErrorAction)
    $rule = $script:Rules | Where-Object Name -eq $Name
    $rule.Application.Program = $Program; $rule.Enabled = $Enabled
    $rule.AddressFilter.LocalAddress = $LocalAddress
    $rule.AddressFilter.RemoteAddress = $RemoteAddress
}
function Disable-NetFirewallRule {
    param($Name, $PolicyStore)
    if ($script:FailDisable) { throw 'simulated policy update failure' }
    ($script:Rules | Where-Object { $_.Name -like $Name }).Enabled = 'False'
}
function Enable-NetFirewallRule {
    param($Name, $PolicyStore, $ErrorAction)
    ($script:Rules | Where-Object { $_.Name -like $Name }).Enabled = 'True'
}
function Remove-NetFirewallRule {
    param($Name, $PolicyStore, $ErrorAction)
    $script:Rules = @($script:Rules | Where-Object { $_.Name -notlike $Name })
}

$request = [pscustomobject]@{
    installation_id=('a' * 32); host_address='172.27.32.1'; guest_address='172.27.44.68'
    port=57880
}
$script:Profiles = @([pscustomobject]@{
    Name='Private'; Enabled='True'; AllowInboundRules='True'
    AllowLocalFirewallRules='True'; DefaultInboundAction='Block'
})
$script:FailDisable = $false
$unrelated = New-TestRule 'another-python' 'C:\another-app\python.exe'
$udp = New-TestRule 'same-python-udp'; $udp.PortFilter.Protocol = 'UDP'
$script:Rules = @((New-TestRule 'matching-tcp'), $unrelated, $udp)
$plan = Get-FirewallPlan $request
Assert-Equal @($plan.blocks) @('matching-tcp') 'Only the exact TCP program block should change'
Assert-Equal $plan.repair $true 'A matching block requires repair'
Invoke-FirewallRepair $request $plan
Assert-Equal (Get-FirewallPlan $request).repair $false 'Effective policy must allow custody'
Assert-Equal $unrelated.Enabled 'True' 'Unrelated Python rules must remain enabled'
Assert-Equal $udp.Enabled 'True' 'UDP rules must remain enabled'
$allow = $script:Rules | Where-Object Action -eq 'Allow'
Assert-Equal $allow.Application.Program 'C:\uv\python.exe' 'Allow must use actual listener executable'
Assert-Equal $allow.AddressFilter.LocalAddress $request.host_address 'Allow needs exact host address'
Assert-Equal $allow.AddressFilter.RemoteAddress $request.guest_address 'Allow needs exact WSL peer'
Assert-Equal $allow.PortFilter.LocalPort 'Any' 'Random TCP ports must not require new UAC each boot'
$request.port = 49510
Assert-Equal (Get-FirewallPlan $request).repair $false 'A new ephemeral port keeps the same allowance'
$request.guest_address = '172.27.44.69'
Assert-Equal (Get-FirewallPlan $request).repair $true 'A changed WSL peer needs an updated allowance'
Invoke-FirewallRepair $request (Get-FirewallPlan $request)
Assert-Equal (Get-FirewallPlan $request).repair $false 'Owned rule must track the new WSL peer'
Assert-Equal @($script:Rules | Where-Object Action -eq 'Allow').Count 1 'Repair must update its own rule'
$request.guest_address = '172.27.44.68'

foreach ($source in @('GroupPolicy', 'Generated')) {
    $block = New-TestRule 'managed-block'; $block.PolicyStoreSourceType = $source
    $script:Rules = @($block)
    Assert-Throws { Get-FirewallPlan $request } 'custom or managed firewall block'
    Assert-Equal $block.Enabled 'True' 'Policy checks must be read-only'
}
$script:Rules = @((New-TestRule 'generic-block' 'Any'))
Assert-Throws { Get-FirewallPlan $request } 'custom or managed firewall block'
$block = New-TestRule 'scoped-block'; $block.AddressFilter.RemoteAddress = '172.27.0.0/16'
$script:Rules = @($block)
Assert-Throws { Get-FirewallPlan $request } 'custom or managed firewall block'
$block.AddressFilter.RemoteAddress = '10.0.0.0/8'
Assert-Equal @((Get-FirewallPlan $request).blocks) @() 'Unrelated address scope must be ignored'
$script:Profiles[0].AllowLocalFirewallRules = 'False'
Assert-Throws { Get-FirewallPlan $request } 'policy disables local inbound exceptions'
$script:Profiles[0].AllowLocalFirewallRules = 'True'

$script:Rules = @((New-TestRule 'block[1]'), (New-TestRule 'block1'))
$plan = Get-FirewallPlan $request
$script:FailDisable = $true
Assert-Throws { Invoke-FirewallRepair $request $plan } 'simulated policy update failure'
Assert-Equal @($script:Rules.Name) @('block[1]', 'block1') 'Failed repair must remove its new allowance'
$script:FailDisable = $false
Invoke-FirewallRepair $request $plan
Assert-Equal @($script:Rules | Where-Object Action -eq 'Block' | ForEach-Object Enabled) `
    @('False', 'False') 'Rule identifiers containing wildcards must be matched literally'

Assert-Equal (Test-AddressScope @('172.27.0.0/16') '172.27.32.1') 1 'CIDR scope matching failed'
Assert-Equal (Test-AddressScope @('172.27.32.2-172.27.32.9') '172.27.32.1') 0 'Range exclusion failed'
Assert-Equal (Test-AddressScope @('LocalSubnet') '172.27.32.1') (-1) 'Dynamic scope must stay unknown'
Assert-Equal (Test-PortScope @('49152-65535') 57880) 1 'Port range matching failed'

# A database relay has an independent stable rule. Original requests without a
# purpose keep custody's previously installed rule identity exactly.
$oldIdentity = Get-FirewallIdentity $request
Assert-Equal $oldIdentity.name ('Potbot-Custody-' + $request.installation_id) 'Custody identity changed'
Assert-Equal $oldIdentity.description ('Potbot managed WSL custody firewall v1; host ' +
    $request.installation_id) 'Custody description changed'
$custodyRule = $script:Rules | Where-Object Name -eq $oldIdentity.name
$script:LiveProgram = 'C:\relay\python.exe'
$databaseRequest = [pscustomobject]@{
    purpose='database'; installation_id=$request.installation_id
    host_address=$request.host_address; guest_address=$request.guest_address; port=53117
}
$databasePlan = Get-FirewallPlan $databaseRequest
Assert-Equal $databasePlan.name ('Potbot-Database-' + $request.installation_id) 'Database rule identity'
Assert-Equal $databasePlan.profile 'potbot.database-firewall/1' 'Database result profile'
Invoke-FirewallRepair $databaseRequest $databasePlan
$databaseRule = $script:Rules | Where-Object Name -eq $databasePlan.name
Assert-Equal $databaseRule.DisplayName 'Potbot WSL Database' 'Database display name'
Assert-Equal $databaseRule.Group 'Potbot WSL database' 'Database rule group'
Assert-Equal $databaseRule.Application.Program 'C:\relay\python.exe' 'Database exact listener program'
Assert-Equal $custodyRule.Application.Program 'C:\uv\python.exe' 'Database repair changed custody'
Assert-Equal @($script:Rules | Where-Object Action -eq 'Allow').Count 2 'Purpose rules must coexist'
$databaseRequest.port = 53118
Assert-Equal (Get-FirewallPlan $databaseRequest).repair $false 'Database ephemeral port should reuse rule'
$databaseRequest.purpose = 'arbitrary'
Assert-Throws { Get-FirewallPlan $databaseRequest } 'Invalid managed firewall purpose'
$script:LiveProgram = 'C:\uv\python.exe'

# A delayed UAC approval cannot repair an abandoned startup or an unrelated
# process that happens to reuse the same port/PID.
$script:StartTime = [datetime]'2026-11-25T03:00:00Z'
$script:ListenerImage = 'C:\uv\python.exe'
$script:Ancestor = 800
function Get-Process { [pscustomobject]@{ StartTime=$script:StartTime } }
function Get-NetTCPConnection { [pscustomobject]@{ OwningProcess=900 } }
function Get-CimInstance {
    param($Class, $Filter)
    if ($Filter -eq 'ProcessId=900') {
        [pscustomobject]@{
            ExecutablePath=$script:ListenerImage; ProcessId=900; ParentProcessId=$script:Ancestor
        }
    } elseif ($Filter -eq 'ProcessId=800') {
        [pscustomobject]@{ ExecutablePath='C:\venv\python.exe'; ProcessId=800; ParentProcessId=700 }
    }
}
$live = [pscustomobject]@{
    host_address='172.27.32.1'; port=57880; controller_pid=700; authority_pid=800
    installation_id=('a' * 32)
    controller_created=$script:StartTime.ToUniversalTime().ToFileTimeUtc().ToString()
    programs=@('C:\venv\python.exe', 'C:\uv\python.exe')
}
Assert-Equal (& $script:ValidateEndpoint $live) 'C:\uv\python.exe' 'Resolve actual child Python'
$script:StartTime = $script:StartTime.AddSeconds(1)
Assert-Throws { & $script:ValidateEndpoint $live } 'startup owner has changed'
$script:StartTime = $script:StartTime.AddSeconds(-1)
$script:ListenerImage = 'C:\another-app\python.exe'
Assert-Throws { & $script:ValidateEndpoint $live } 'prepared Python runtime'
$script:ListenerImage = 'C:\uv\python.exe'
$script:Ancestor = 999
Assert-Throws { & $script:ValidateEndpoint $live } 'does not belong to this Potbot startup'
$live | Add-Member -NotePropertyName purpose -NotePropertyValue 'database'
$live.controller_pid = 900
$live.authority_pid = 900
Assert-Equal (& $script:ValidateEndpoint $live) 'C:\uv\python.exe' 'Same-process relay ownership'
$script:StartTime = $script:StartTime.AddSeconds(1)
Assert-Throws { & $script:ValidateEndpoint $live } 'startup owner has changed'
Write-Output 'Custody firewall policy tests passed.'
