param(
    [string]$RequestBase64,
    [ValidateSet('Inspect', 'Repair')][string]$Mode = 'Inspect',
    [string]$ResultPipe
)

# This helper is encoded and passed directly to Windows PowerShell. Elevated
# execution receives only public process/network identity, never custody tokens.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)

function ConvertTo-IPv4Number([string]$Value) {
    $address = $null
    if (-not [Net.IPAddress]::TryParse($Value, [ref]$address) -or
        $address.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork) { return $null }
    $bytes = $address.GetAddressBytes()
    [Array]::Reverse($bytes)
    return [uint64][BitConverter]::ToUInt32($bytes, 0)
}

function Test-AddressScope($Values, [string]$Address) {
    $number = ConvertTo-IPv4Number $Address
    $unknown = $false
    foreach ($value in @($Values)) {
        if ($value -eq 'Any' -or $value -eq '*') { return 1 }
        if ($value -eq $Address) { return 1 }
        if ($value -match '^([^/]+)/([0-9]{1,2})$') {
            $start = ConvertTo-IPv4Number $Matches[1]
            $bits = [int]$Matches[2]
            if ($null -ne $start -and $bits -le 32) {
                $size = [uint64][Math]::Pow(2, 32 - $bits)
                $first = $start - ($start % $size)
                if ($number -ge $first -and $number -lt ($first + $size)) { return 1 }
                continue
            }
        }
        if ($value -match '^([0-9.]+)-([0-9.]+)$') {
            $first = ConvertTo-IPv4Number $Matches[1]
            $last = ConvertTo-IPv4Number $Matches[2]
            if ($null -ne $first -and $null -ne $last) {
                if ($number -ge $first -and $number -le $last) { return 1 }
                continue
            }
        }
        $parsed = $null
        if ([Net.IPAddress]::TryParse(($value -split '/')[0], [ref]$parsed)) { continue }
        $unknown = $true
    }
    if ($unknown) { return -1 }
    return 0
}

function Test-PortScope($Values, [int]$Port) {
    foreach ($value in @($Values)) {
        if ($value -eq 'Any' -or $value -eq '*') { return 1 }
        if ($value -match '^[0-9]+$' -and [int]$value -eq $Port) { return 1 }
        if ($value -match '^([0-9]+)-([0-9]+)$') {
            if ($Port -ge [int]$Matches[1] -and $Port -le [int]$Matches[2]) { return 1 }
        } elseif ($value -notmatch '^[0-9]+$') { return -1 }
    }
    return 0
}

function Test-Any($Values) {
    $items = @($Values)
    return $items.Count -eq 1 -and ($items[0] -eq 'Any' -or $items[0] -eq '*')
}

function Get-FirewallIdentity($Request) {
    # Missing purpose is the original custody request contract. Keep its exact
    # rule identity so an upgrade reuses the rule already approved by the user.
    $purpose = 'custody'
    if ($Request.PSObject.Properties['purpose']) { $purpose = [string]$Request.purpose }
    if ($purpose -cnotin @('custody', 'database')) { throw 'Invalid managed firewall purpose' }
    $title = if ($purpose -eq 'custody') { 'Custody' } else { 'Database' }
    return @{
        purpose=$purpose; profile=('potbot.' + $purpose + '-firewall/1')
        name=('Potbot-' + $title + '-' + $Request.installation_id)
        description=('Potbot managed WSL ' + $purpose + ' firewall v1; host ' + $Request.installation_id)
        group=('Potbot WSL ' + $purpose); display_name=('Potbot WSL ' + $title)
    }
}

function Assert-LiveEndpoint($Request) {
    $purpose = (Get-FirewallIdentity $Request).purpose
    $controller = Get-Process -Id $Request.controller_pid -ErrorAction Stop
    if ($controller.StartTime.ToUniversalTime().ToFileTimeUtc().ToString() -cne
        $Request.controller_created) { throw 'Potbot startup owner has changed' }
    $listeners = @(Get-NetTCPConnection -State Listen -LocalAddress $Request.host_address `
        -LocalPort $Request.port -ErrorAction SilentlyContinue)
    if ($listeners.Count -ne 1) { throw ('Potbot ' + $purpose + ' listener is not ready or is ambiguous') }
    $process = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $listeners[0].OwningProcess)
    $program = [string]$process.ExecutablePath
    if (-not $program -or $Request.programs -notcontains $program) {
        throw ('Potbot ' + $purpose + ' listener does not belong to the prepared Python runtime')
    }
    $found = $false
    for ($i = 0; $i -lt 5 -and $process; $i++) {
        if ($process.ProcessId -eq $Request.authority_pid) { $found = $true; break }
        $process = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $process.ParentProcessId)
    }
    if (-not $found) { throw ('Potbot ' + $purpose + ' listener does not belong to this Potbot startup') }
    return $program
}

function Get-FirewallPlan($Request) {
    $identity = Get-FirewallIdentity $Request
    $program = Assert-LiveEndpoint $Request
    $name = $identity.name
    $description = $identity.description
    $own = @(Get-NetFirewallRule -PolicyStore PersistentStore -Name $name `
        -ErrorAction SilentlyContinue)
    if ($own.Count -gt 1 -or ($own.Count -eq 1 -and
        ($own[0].Description -cne $description -or $own[0].Group -cne $identity.group))) {
        throw 'The managed Potbot firewall rule name is already owned by another rule'
    }
    $ip = Get-NetIPAddress -IPAddress $Request.host_address -AddressFamily IPv4
    $network = @(Get-NetConnectionProfile -InterfaceIndex $ip.InterfaceIndex `
        -ErrorAction SilentlyContinue)
    $mask = 0
    foreach ($item in $network) {
        switch ([string]$item.NetworkCategory) {
            'DomainAuthenticated' { $mask = $mask -bor 1 }
            'Private' { $mask = $mask -bor 2 }
            'Public' { $mask = $mask -bor 4 }
        }
    }
    # WSL's virtual adapter may have no NetworkListManager profile of its own.
    if (-not $mask) { $mask = (New-Object -ComObject HNetCfg.FwPolicy2).CurrentProfileTypes }
    $enabled = 0
    $defaultAllow = 0
    foreach ($profile in @(Get-NetFirewallProfile -PolicyStore ActiveStore)) {
        $bit = switch ([string]$profile.Name) { 'Domain' {1} 'Private' {2} 'Public' {4} }
        if (-not ($mask -band $bit)) { continue }
        if ([string]$profile.Enabled -eq 'False') { $defaultAllow = $defaultAllow -bor $bit; continue }
        $enabled = $enabled -bor $bit
        if ([string]$profile.AllowInboundRules -eq 'False' -or
            [string]$profile.AllowLocalFirewallRules -eq 'False') {
            throw 'Windows policy disables local inbound exceptions; ask its administrator'
        }
        if ([string]$profile.DefaultInboundAction -eq 'Allow') {
            $defaultAllow = $defaultAllow -bor $bit
        }
    }
    $blocks = @()
    $allowed = $defaultAllow
    $applications = @(Get-NetFirewallApplicationFilter -PolicyStore ActiveStore |
        Where-Object {
            $candidate = [Environment]::ExpandEnvironmentVariables([string]$_.Program)
            $candidate -eq $program -or $candidate -eq 'Any'
        })
    $rules = @($applications | Get-NetFirewallRule -PolicyStore ActiveStore -TracePolicyStore |
        Where-Object { [string]$_.Enabled -eq 'True' -and $_.Direction -eq 'Inbound' })
    foreach ($rule in $rules) {
        $profiles = [int]$rule.Profile
        if ($profiles -eq 0) { $profiles = 7 }
        if (-not ($profiles -band $enabled)) { continue }
        $app = $rule | Get-NetFirewallApplicationFilter
        $candidate = [Environment]::ExpandEnvironmentVariables([string]$app.Program)
        if ($candidate -ne $program -and $candidate -ne 'Any') { continue }
        $service = $rule | Get-NetFirewallServiceFilter
        if ($service.Service -ne 'Any' -or $app.Package -notin @('Any', $null, '')) { continue }
        $port = $rule | Get-NetFirewallPortFilter
        if ([string]$port.Protocol -notin @('TCP', '6', 'Any', '256')) { continue }
        $addresses = $rule | Get-NetFirewallAddressFilter
        $local = Test-AddressScope $addresses.LocalAddress $Request.host_address
        $remote = Test-AddressScope $addresses.RemoteAddress $Request.guest_address
        $localPort = Test-PortScope $port.LocalPort $Request.port
        if ($local -eq 0 -or $remote -eq 0 -or $localPort -eq 0) { continue }
        $interface = $rule | Get-NetFirewallInterfaceFilter
        $types = $rule | Get-NetFirewallInterfaceTypeFilter
        $security = $rule | Get-NetFirewallSecurityFilter
        $simple = (Test-Any $port.RemotePort) -and
            (Test-Any $interface.InterfaceAlias) -and $types.InterfaceType -eq 'Any' -and
            $security.Authentication -eq 'NotRequired' -and
            $security.LocalUser -eq 'Any' -and $security.RemoteUser -eq 'Any' -and
            $security.RemoteMachine -eq 'Any' -and $local -eq 1 -and $remote -eq 1 -and
            $localPort -eq 1
        if ([string]$rule.Action -eq 'Block') {
            # Only ordinary local per-application block rules are auto-repaired.
            # Custom scopes and broad/enterprise rules need their administrator.
            if (-not $simple -or $candidate -ne $program -or
                [string]$rule.PolicyStoreSourceType -ne 'Local' -or
                -not (Test-Any $addresses.LocalAddress) -or
                -not (Test-Any $addresses.RemoteAddress) -or -not (Test-Any $port.LocalPort)) {
                throw ('A custom or managed firewall block prevents ' + $identity.purpose + ': ' + $rule.DisplayName)
            }
            $localRule = @(Get-NetFirewallRule -PolicyStore PersistentStore `
                -Name ([Management.Automation.WildcardPattern]::Escape($rule.Name)) `
                -ErrorAction SilentlyContinue)
            if ($localRule.Count -ne 1) { throw 'An effective block cannot be repaired locally' }
            $blocks += $rule.Name
        } elseif ($simple -and [string]$rule.Action -eq 'Allow') {
            $allowed = $allowed -bor ($profiles -band $mask)
        }
    }
    return @{
        profile = $identity.profile; program = $program; name = $name
        description = $description; blocks = @($blocks); own = $own
        group = $identity.group; display_name = $identity.display_name
        repair = ($blocks.Count -gt 0 -or ($allowed -band $mask) -ne $mask)
    }
}

function Invoke-FirewallRepair($Request, $Plan) {
    $changed = @()
    $created = $false
    $previous = $null
    if ($Plan.own.Count -eq 1) {
        $rule = $Plan.own[0]
        $previous = @{
            Program = ($rule | Get-NetFirewallApplicationFilter).Program
            LocalAddress = ($rule | Get-NetFirewallAddressFilter).LocalAddress
            RemoteAddress = ($rule | Get-NetFirewallAddressFilter).RemoteAddress
            Enabled = $rule.Enabled
        }
        # Do not overwrite an administrator's customisation of our rule.
        $port = $rule | Get-NetFirewallPortFilter
        if ($rule.Action -ne 'Allow' -or $rule.Direction -ne 'Inbound' -or
            [int]$rule.Profile -ne 0 -or [string]$port.Protocol -notin @('TCP', '6') -or
            -not (Test-Any $port.LocalPort) -or -not (Test-Any $port.RemotePort)) {
            throw 'The managed Potbot firewall rule was customised; review it manually'
        }
    }
    try {
        $null = Assert-LiveEndpoint $Request
        $parameters = @{
            PolicyStore='PersistentStore'; Name=$Plan.name; Enabled='True'; Action='Allow'
            Direction='Inbound'; Profile='Any'; Protocol='TCP'; LocalPort='Any'
            RemotePort='Any'; Program=$Plan.program; LocalAddress=$Request.host_address
            RemoteAddress=$Request.guest_address; EdgeTraversalPolicy='Block'
        }
        if ($previous) {
            Set-NetFirewallRule -PolicyStore PersistentStore -Name $Plan.name `
                -Program $Plan.program -LocalAddress $Request.host_address `
                -RemoteAddress $Request.guest_address -Enabled True | Out-Null
        } else {
            New-NetFirewallRule @parameters -DisplayName $Plan.display_name `
                -Description $Plan.description -Group $Plan.group | Out-Null
            $created = $true
        }
        foreach ($name in $Plan.blocks) {
            Disable-NetFirewallRule -PolicyStore PersistentStore `
                -Name ([Management.Automation.WildcardPattern]::Escape($name)) | Out-Null
            $changed += $name
        }
        # Retry only an incomplete effective-policy view. Validation and policy
        # errors must still abort immediately, without weakening the exception.
        for ($attempt = 0; $attempt -lt 4; $attempt++) {
            $verified = Get-FirewallPlan $Request
            if (-not $verified.repair) { return $verified }
            if ($attempt -lt 3) { Start-Sleep -Milliseconds 250 }
        }
        throw 'Firewall repair is not effective after verification'
    } catch {
        $repairFailure = $_
        $rollbackFailures = @()
        foreach ($name in $changed) {
            try {
                Enable-NetFirewallRule -PolicyStore PersistentStore `
                    -Name ([Management.Automation.WildcardPattern]::Escape($name)) `
                    -ErrorAction Stop | Out-Null
            } catch {
                $rollbackFailures += ('Restore block ' + $name + ': ' + $_.Exception.Message)
            }
        }
        if ($created) {
            try {
                Remove-NetFirewallRule -PolicyStore PersistentStore -Name $Plan.name `
                    -ErrorAction Stop | Out-Null
            } catch {
                $rollbackFailures += ('Remove allowance ' + $Plan.name + ': ' + $_.Exception.Message)
            }
        } elseif ($previous) {
            try {
                Set-NetFirewallRule -PolicyStore PersistentStore -Name $Plan.name @previous `
                    -ErrorAction Stop | Out-Null
            } catch {
                $rollbackFailures += ('Restore allowance ' + $Plan.name + ': ' + $_.Exception.Message)
            }
        }
        if ($rollbackFailures.Count) {
            $repairFailure.Exception.Data['PotbotFirewallRollbackErrors'] = $rollbackFailures
        }
        throw $repairFailure
    }
}

if ($RequestBase64) {
    $resultStream = $null
    $resultWriter = $null
    $receipt = $null
    $exitCode = 0
    $purpose = 'custody'
    $operation = 'connect-result'
    try {
        if ($ResultPipe) {
            if ($Mode -ne 'Repair' -or $ResultPipe -cnotmatch '^potbot-firewall-[0-9a-f]{32}$') {
                throw 'Invalid firewall result channel'
            }
            # Connect before any firewall mutation. A late UAC acceptance after
            # the unelevated launcher has gone away cannot perform a repair.
            # The client receives no commands; the server cannot impersonate it.
            $resultStream = New-Object IO.Pipes.NamedPipeClientStream('.', $ResultPipe,
                [IO.Pipes.PipeAccessRights]::WriteData, [IO.Pipes.PipeOptions]::None,
                [Security.Principal.TokenImpersonationLevel]::Anonymous,
                [IO.HandleInheritability]::None)
            $resultStream.Connect(10000)
            $resultWriter = New-Object IO.StreamWriter($resultStream, (New-Object Text.UTF8Encoding($false)))
        }
        $operation = 'validate-request'
        $request = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($RequestBase64)) |
            ConvertFrom-Json
        $purpose = (Get-FirewallIdentity $request).purpose
        if ($request.installation_id -notmatch '^[0-9a-f]{32}$' -or
            $request.controller_created -notmatch '^[0-9]{10,20}$' -or
            $request.controller_pid -lt 1 -or $request.authority_pid -lt 1 -or
            $request.port -lt 1 -or $request.port -gt 65535) { throw 'Invalid firewall request' }
        foreach ($value in @($request.host_address, $request.guest_address)) {
            $ip = [Net.IPAddress]::Parse($value)
            if ($ip.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork -or
                $value -notmatch '^(10\.|172\.(1[6-9]|2[0-9]|3[01])\.|192\.168\.)') {
                throw 'Firewall exception requires private WSL IPv4 addresses'
            }
        }
        $operation = 'inspect-policy'
        $plan = Get-FirewallPlan $request
        if ($Mode -eq 'Repair' -and $plan.repair) {
            $operation = 'repair-policy'
            $plan = Invoke-FirewallRepair $request $plan
        }
        if ($resultWriter) {
            $receipt = @{
                profile='potbot.firewall-result/1'; purpose=$purpose; exit_code=0
                operation='complete'; message=''; repair=$plan.repair
            }
        } else {
            @{profile=$plan.profile; repair=$plan.repair; program=$plan.program;
                blocks=@($plan.blocks)} | ConvertTo-Json -Compress
        }
    } catch {
        $errorRecord = $_
        $failure = $errorRecord.Exception
        $exitCode = 1
        while ($failure) {
            if ($failure -is [UnauthorizedAccessException] -or
                ($failure.HResult -band 0xFFFFFFFFL) -eq 0x80070005L -or
                ($failure -is [ComponentModel.Win32Exception] -and
                    $failure.NativeErrorCode -eq 5) -or
                ($failure.GetType().FullName -eq 'Microsoft.Management.Infrastructure.CimException' -and
                    [string]$failure.NativeErrorCode -eq 'AccessDenied')) { $exitCode = 5; break }
            $failure = $failure.InnerException
        }
        $command = ''
        if ($errorRecord.InvocationInfo -and $errorRecord.InvocationInfo.MyCommand) {
            $command = [string]$errorRecord.InvocationInfo.MyCommand.Name
        }
        $message = $errorRecord.Exception.Message + ' [' + $errorRecord.FullyQualifiedErrorId +
            '; HRESULT=0x' + ($errorRecord.Exception.HResult -band 0xFFFFFFFFL).ToString('X8') + ']'
        if ($command) { $message = $command + ': ' + $message }
        if ($errorRecord.Exception.Data.Contains('PotbotFirewallRollbackErrors')) {
            $message += '; rollback: ' + ($errorRecord.Exception.Data['PotbotFirewallRollbackErrors'] -join '; ')
        }
        # These diagnostics contain only firewall/process identity, never the
        # database DSN, authority token or vault contents. JSON may escape each
        # code unit into six bytes; reserve room within the 16 KiB pipe bound.
        if ($message.Length -gt 2000) { $message = $message.Substring(0, 2000) }
        [Console]::Error.WriteLine($message)
        $receipt = @{
            profile='potbot.firewall-result/1'; purpose=$purpose; exit_code=$exitCode
            operation=$operation; message=$message; repair=$true
        }
    } finally {
        try {
            if ($resultWriter -and $receipt) {
                $resultWriter.WriteLine(($receipt | ConvertTo-Json -Compress))
                $resultWriter.Flush()
            }
        } catch {
            [Console]::Error.WriteLine('Cannot return the firewall repair result: ' + $_.Exception.Message)
            $exitCode = 1
        } finally {
            if ($resultWriter) { $resultWriter.Dispose() }
            if ($resultStream) { $resultStream.Dispose() }
        }
    }
    exit $exitCode
}
